package server

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
)

func (s *service) getConfig(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{
		"whisperx":              s.whisperxEnabled,
		"funasr":                s.funasrEnabled,
		"funasrrealtime":        s.funasrRealtimeEnabled,
		"whisperx_single_model": s.whisperxSingleModel,
	})
}

func (s *service) health(c *gin.Context) {
	c.Status(http.StatusNoContent)
}

type modelEntry struct {
	ID      string `json:"id"`
	Object  string `json:"object"`
	OwnedBy string `json:"owned_by"`
	Baked   *bool  `json:"baked,omitempty"`
	Loaded  *bool  `json:"loaded,omitempty"`
	Ready   *bool  `json:"ready,omitempty"`
}

func (s *service) models(c *gin.Context) {
	type upstreamResult struct {
		entries []modelEntry
		err     error
		url     string
	}
	results := make(chan upstreamResult, len(s.upstreams))
	for _, backend := range s.upstreams {
		go func(backend upstream) {
			remote, err := s.upstreamModels(c.Request.Context(), backend)
			results <- upstreamResult{entries: remote, err: err, url: backend.url}
		}(backend)
	}

	entries := make([]modelEntry, 0)
	upstreamFailed := false
	for range s.upstreams {
		result := <-results
		if result.err != nil {
			log.Printf("list models from %s: %v", result.url, result.err)
			upstreamFailed = true
			continue
		}
		entries = append(entries, result.entries...)
	}
	if upstreamFailed {
		c.JSON(http.StatusBadGateway, gin.H{"error": "failed to list models from upstream services"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"object": "list", "data": entries})
}

func (s *service) upstreamModels(parentContext context.Context, backend upstream) ([]modelEntry, error) {
	requestContext, cancel := context.WithTimeout(parentContext, 5*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(requestContext, http.MethodGet, backend.url+"/v1/models", nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+backend.token)
	response, err := s.statusClient.Do(request)
	if err != nil {
		return nil, err
	}
	defer func() {
		if err := response.Body.Close(); err != nil {
			log.Printf("close upstream models response: %v", err)
		}
	}()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return nil, fmt.Errorf("upstream returned %d", response.StatusCode)
	}
	var payload struct {
		Data []modelEntry `json:"data"`
	}
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		return nil, err
	}
	return payload.Data, nil
}

func (s *service) system(c *gin.Context) {
	whisperXStatus := make(chan string, 1)
	funASRStatus := make(chan string, 1)
	go func() {
		if s.whisperxEnabled {
			whisperXStatus <- s.upstreamStatus(c.Request.Context(), s.upstreams["whisperx"])
		} else {
			whisperXStatus <- "disabled"
		}
	}()
	go func() {
		if s.funasrEnabled {
			funASRStatus <- s.upstreamStatus(c.Request.Context(), s.upstreams["funasr"])
		} else {
			funASRStatus <- "disabled"
		}
	}()

	modelsList := make([]gin.H, 0)
	if s.whisperxEnabled {
		modelsList = append(modelsList, gin.H{"id": "whisperx", "upstream_status": <-whisperXStatus})
	} else {
		<-whisperXStatus
	}
	if s.funasrEnabled {
		modelsList = append(modelsList, gin.H{"id": "funasr", "upstream_status": <-funASRStatus})
	} else {
		<-funASRStatus
	}

	c.JSON(http.StatusOK, gin.H{
		"status": "ok",
		"models": modelsList,
	})
}

func (s *service) upstreamStatus(parentContext context.Context, backend upstream) string {
	requestContext, cancel := context.WithTimeout(parentContext, 5*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(requestContext, http.MethodGet, backend.url+"/health", nil)
	if err != nil {
		log.Printf("build upstream health request: %v", err)
		return "unavailable"
	}
	request.Header.Set("Authorization", "Bearer "+backend.token)
	response, err := s.statusClient.Do(request)
	if err != nil {
		return "unavailable"
	}
	defer func() {
		if err := response.Body.Close(); err != nil {
			log.Printf("close upstream health response: %v", err)
		}
	}()
	if _, err := io.Copy(io.Discard, response.Body); err != nil {
		log.Printf("read upstream health response: %v", err)
		return "unavailable"
	}
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return "unavailable"
	}
	return "available"
}

func (s *service) authenticate(c *gin.Context) {
	if c.GetHeader("Authorization") != "Bearer "+s.token {
		c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "unauthorized"})
		return
	}
	c.Next()
}
