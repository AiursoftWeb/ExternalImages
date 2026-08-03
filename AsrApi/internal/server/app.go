package server

import (
	"embed"
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/joho/godotenv"
)

type service struct {
	token                 string
	upstreams             map[string]upstream
	client                *http.Client
	statusClient          *http.Client
	whisperxEnabled       bool
	funasrEnabled         bool
	funasrRealtimeEnabled bool
	whisperxSingleModel   bool
	taskManager           *TaskManager
	uploadSem             chan struct{}
	transcribeSem         chan struct{}
}

type serviceEnvironment struct {
	whisperxEnabled             bool
	funasrEnabled               bool
	funasrRealtimeEnabled       bool
	whisperxSingleModel         bool
	maxStoredAudioSizeMiB       int
	maxConcurrentUploads        int
	maxConcurrentTranscriptions int
}

func Run(distFS embed.FS) error {
	if err := loadDotenv(".env"); err != nil {
		return err
	}

	server, err := newServiceFromEnvironment()
	if err != nil {
		return err
	}

	router, err := newRouter(server, distFS)
	if err != nil {
		return err
	}

	return router.Run(":" + environmentOrDefault("PORT", "8080"))
}

func loadDotenv(filename string) error {
	if err := godotenv.Load(filename); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func newRouter(server *service, distFS embed.FS) (*gin.Engine, error) {
	router := gin.New()
	router.Use(gin.Logger(), gin.Recovery())
	dist, err := fs.Sub(distFS, "web/dist")
	if err != nil {
		return nil, err
	}
	assetsFS, err := fs.Sub(dist, "assets")
	if err != nil {
		return nil, err
	}
	router.StaticFS("/assets", http.FS(assetsFS))
	router.GET("/", func(c *gin.Context) {
		data, err := fs.ReadFile(dist, "index.html")
		if err != nil {
			c.Status(http.StatusInternalServerError)
			return
		}
		c.Data(http.StatusOK, "text/html; charset=utf-8", data)
	})
	router.GET("/healthz", server.health)
	router.GET("/config", server.getConfig)
	v1 := router.Group("/v1")
	v1.Use(server.authenticate)
	v1.GET("/models", server.models)
	v1.GET("/system", server.system)
	v1.POST("/audio/transcriptions", server.transcribe)
	v1.POST("/tasks/cancel", server.cancelTask)
	v1.POST("/tasks/:id/cancel", server.cancelTask)
	return router, nil
}

func newServiceFromEnvironment() (*service, error) {
	token := os.Getenv("ASR_API_TOKEN")
	if token == "" {
		return nil, errors.New("ASR_API_TOKEN is required")
	}

	settings, err := loadServiceEnvironment()
	if err != nil {
		return nil, err
	}
	if !settings.whisperxEnabled && !settings.funasrEnabled {
		return nil, errors.New("at least one of ASR_ENABLE_WHISPERX or ASR_ENABLE_FUNASR must be true")
	}

	var whisperXURL, whisperXToken string
	if settings.whisperxEnabled {
		whisperXToken = os.Getenv("ASR_WHISPERX_TOKEN")
		if whisperXToken == "" {
			return nil, errors.New("ASR_WHISPERX_TOKEN is required when whisperx is enabled")
		}
		whisperXURL = strings.TrimRight(os.Getenv("ASR_WHISPERX_URL"), "/")
		if whisperXURL == "" {
			return nil, errors.New("ASR_WHISPERX_URL is required when whisperx is enabled")
		}
		if err := validateUpstreamURL(whisperXURL); err != nil {
			return nil, fmt.Errorf("ASR_WHISPERX_URL %w", err)
		}
	}

	var funASRURL, funASRToken string
	if settings.funasrEnabled {
		funASRToken = os.Getenv("ASR_FUNASR_TOKEN")
		if funASRToken == "" {
			return nil, errors.New("ASR_FUNASR_TOKEN is required when funasr is enabled")
		}
		funASRURL = strings.TrimRight(os.Getenv("ASR_FUNASR_URL"), "/")
		if funASRURL == "" {
			return nil, errors.New("ASR_FUNASR_URL is required when funasr is enabled")
		}
		if err := validateUpstreamURL(funASRURL); err != nil {
			return nil, fmt.Errorf("ASR_FUNASR_URL %w", err)
		}
	}

	upstreams := make(map[string]upstream)
	if settings.whisperxEnabled {
		upstreams["whisperx"] = upstream{
			url:   whisperXURL,
			model: environmentOrDefault("ASR_WHISPERX_MODEL", "large-v3"),
			token: whisperXToken,
		}
	}
	if settings.funasrEnabled {
		upstreams["funasr"] = upstream{
			url:   funASRURL,
			model: environmentOrDefault("ASR_FUNASR_MODEL", "sensevoice"),
			token: funASRToken,
		}
	}

	maxStoredBytes := int64(settings.maxStoredAudioSizeMiB) << 20
	tm := NewTaskManager(16, maxStoredBytes, upstreams)
	// 每个模型只有一个串行 worker；此限制仅约束不同模型同时占用的全局资源。
	s := &service{
		token:                 token,
		upstreams:             upstreams,
		client:                &http.Client{Timeout: 10 * time.Minute},
		statusClient:          &http.Client{Timeout: cancelUpstreamTimeout},
		whisperxEnabled:       settings.whisperxEnabled,
		funasrEnabled:         settings.funasrEnabled,
		funasrRealtimeEnabled: settings.funasrRealtimeEnabled,
		whisperxSingleModel:   settings.whisperxSingleModel,
		taskManager:           tm,
		uploadSem:             make(chan struct{}, settings.maxConcurrentUploads),
		transcribeSem:         make(chan struct{}, settings.maxConcurrentTranscriptions),
	}
	s.startQueueWorkers(tm)
	return s, nil
}

func validateUpstreamURL(value string) error {
	parsed, err := url.ParseRequestURI(value)
	if err != nil {
		return errors.New("must be a valid absolute URL")
	}
	isHTTP := strings.EqualFold(parsed.Scheme, "http") || strings.EqualFold(parsed.Scheme, "https")
	if parsed.Host == "" || !isHTTP {
		return errors.New("must be an absolute HTTP(S) URL")
	}
	if parsed.RawQuery != "" || parsed.Fragment != "" {
		return errors.New("must not contain a query or fragment")
	}
	return nil
}

func loadServiceEnvironment() (serviceEnvironment, error) {
	var settings serviceEnvironment
	var err error
	if settings.whisperxEnabled, err = environmentOrDefaultBool("ASR_ENABLE_WHISPERX", true); err != nil {
		return serviceEnvironment{}, err
	}
	if settings.funasrEnabled, err = environmentOrDefaultBool("ASR_ENABLE_FUNASR", true); err != nil {
		return serviceEnvironment{}, err
	}
	if settings.funasrRealtimeEnabled, err = environmentOrDefaultBool("ASR_ENABLE_FUNASR_REALTIME", true); err != nil {
		return serviceEnvironment{}, err
	}
	if settings.whisperxSingleModel, err = environmentOrDefaultBool("ASR_WHISPERX_SINGLE_MODEL", true); err != nil {
		return serviceEnvironment{}, err
	}
	if settings.maxStoredAudioSizeMiB, err = environmentOrDefaultInt("ASR_MAX_STORED_AUDIO_SIZE_MIB", defaultMaxStoredAudioSizeMiB, maxConfiguredStoredAudioMiB); err != nil {
		return serviceEnvironment{}, err
	}
	if settings.maxConcurrentUploads, err = environmentOrDefaultInt("ASR_MAX_CONCURRENT_UPLOADS", 2, maxConfiguredConcurrency); err != nil {
		return serviceEnvironment{}, err
	}
	if settings.maxConcurrentTranscriptions, err = environmentOrDefaultInt("ASR_MAX_CONCURRENT_TRANSCRIPTIONS", 2, maxConfiguredConcurrency); err != nil {
		return serviceEnvironment{}, err
	}
	return settings, nil
}

func environmentOrDefault(name, defaultValue string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return defaultValue
}

func environmentOrDefaultInt(name string, defaultValue, maxValue int) (int, error) {
	value, exists := os.LookupEnv(name)
	if !exists {
		return defaultValue, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 || parsed > maxValue {
		return 0, fmt.Errorf("%s must be an integer between 1 and %d, got %q", name, maxValue, value)
	}
	return parsed, nil
}

func environmentOrDefaultBool(name string, defaultValue bool) (bool, error) {
	value, exists := os.LookupEnv(name)
	if !exists {
		return defaultValue, nil
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return false, fmt.Errorf("%s must be a boolean, got %q", name, value)
	}
	return parsed, nil
}
