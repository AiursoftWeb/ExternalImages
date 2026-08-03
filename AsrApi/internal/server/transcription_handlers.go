package server

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand"
	"mime/multipart"
	"net/http"
	"os"
	"time"

	"github.com/gin-gonic/gin"
)

var (
	errAudioFileRequired      = errors.New("audio file is required")
	errInvalidMultipartUpload = errors.New("invalid multipart upload")
)

type transcriptionUpload struct {
	Model          string
	Level          string
	Language       string
	ResponseFormat string
	TaskID         string
	Filename       string
	TempFilePath   string
}

func generateTaskID() string {
	return fmt.Sprintf("task_%d_%06d", time.Now().UnixNano(), rand.Intn(1000000))
}

func validateTaskID(id string) error {
	if len(id) == 0 {
		return errors.New("task id is required")
	}
	if len(id) > maxTaskIDLength {
		return fmt.Errorf("task id must not exceed %d characters", maxTaskIDLength)
	}
	for _, char := range id {
		if char >= 'a' && char <= 'z' || char >= 'A' && char <= 'Z' || char >= '0' && char <= '9' {
			continue
		}
		if char == '-' || char == '.' || char == '_' || char == '~' {
			continue
		}
		return errors.New("task id must contain only URL-safe ASCII characters")
	}
	return nil
}

func (s *service) transcribe(c *gin.Context) {
	select {
	case s.uploadSem <- struct{}{}:
	default:
		c.JSON(http.StatusTooManyRequests, gin.H{"error": "too many concurrent uploads, please retry later"})
		return
	}
	uploadSlotHeld := true
	defer func() {
		if uploadSlotHeld {
			<-s.uploadSem
		}
	}()

	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxAudioFileSize+maxUploadRequestOverhead)
	taskID := c.GetHeader("X-Task-Id")
	if taskID != "" {
		if err := validateTaskID(taskID); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
	}

	task := &ASRTask{Status: StatusPending}
	taskStorageHandedOff := false
	defer func() {
		if !taskStorageHandedOff {
			s.taskManager.releaseTaskStorage(task)
		}
	}()
	upload, err := s.readTranscriptionUpload(c.Request, task)
	if err != nil {
		statusCode, message := transcriptionUploadErrorResponse(err)
		if statusCode == http.StatusInternalServerError {
			log.Printf("failed to store uploaded audio: %v", err)
		}
		c.JSON(statusCode, gin.H{"error": message})
		return
	}

	modelName := upload.Model
	_, ok := s.upstreams[modelName]
	if !ok {
		removeTemporaryFile(upload.TempFilePath)
		c.JSON(http.StatusBadRequest, gin.H{"error": fmt.Sprintf("model %s is not supported or not enabled", modelName)})
		return
	}

	if taskID == "" {
		taskID = upload.TaskID
	}
	if taskID == "" {
		taskID = generateTaskID()
	}
	if err := validateTaskID(taskID); err != nil {
		removeTemporaryFile(upload.TempFilePath)
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	taskCtx, taskCancel := context.WithCancel(context.Background())

	task.ID = taskID
	task.Model = modelName
	task.Level = upload.Level
	task.Language = upload.Language
	task.Filename = upload.Filename
	task.TempFilePath = upload.TempFilePath
	task.ResponseFormat = upload.ResponseFormat
	task.ResultChan = make(chan ASRTaskResult, 1)
	task.Ctx = taskCtx
	task.CancelFunc = taskCancel
	task.CreatedAt = time.Now()

	log.Printf("[ASR API] Adding task %s for file %s to queue", task.ID, task.Filename)
	if err := s.taskManager.Add(task); err != nil {
		taskCancel()
		removeTemporaryFile(upload.TempFilePath)
		c.JSON(http.StatusTooManyRequests, gin.H{"error": err.Error()})
		return
	}
	taskStorageHandedOff = true
	<-s.uploadSem
	uploadSlotHeld = false

	c.Header("X-Task-Id", task.ID)

	select {
	case result := <-task.ResultChan:
		if result.Err != nil {
			log.Printf("[ASR API] Task %s failed: %v", task.ID, result.Err)
			if len(result.Body) > 0 {
				c.Header("Content-Type", "application/json")
				c.Status(result.StatusCode)
				if _, err := c.Writer.Write(result.Body); err != nil {
					log.Printf("[ASR API] Failed to write error response for task %s: %v", task.ID, err)
				}
			} else {
				c.JSON(result.StatusCode, gin.H{"error": result.Err.Error()})
			}
			return
		}

		log.Printf("[ASR API] Task %s processed successfully. Status: %d", task.ID, result.StatusCode)
		for k, vv := range result.Header {
			for _, v := range vv {
				c.Header(k, v)
			}
		}
		c.Status(result.StatusCode)
		if _, err := c.Writer.Write(result.Body); err != nil {
			log.Printf("[ASR API] Failed to write response for task %s: %v", task.ID, err)
		}

	case <-c.Request.Context().Done():
		log.Printf("[ASR API] Client disconnected during execution of task %s, triggering cancellation...", task.ID)
		if _, err := s.taskManager.CancelAndWait(task.ID, s.cancelTaskForModel, cancelTaskCleanupTimeout); err != nil {
			log.Printf("[ASR API] Failed to confirm cancellation after client disconnected for task %s: %v", task.ID, err)
		}
		c.JSON(http.StatusRequestTimeout, gin.H{"error": "client disconnected"})
	}
}

func transcriptionUploadErrorResponse(err error) (int, string) {
	var maxBytesError *http.MaxBytesError
	switch {
	case errors.As(err, &maxBytesError):
		return http.StatusRequestEntityTooLarge, fmt.Sprintf("audio file exceeds %d MiB limit", maxAudioFileSize>>20)
	case errors.Is(err, errStoredAudioCapacity):
		return http.StatusTooManyRequests, err.Error()
	case errors.Is(err, errAudioFileRequired):
		return http.StatusBadRequest, errAudioFileRequired.Error()
	case errors.Is(err, errInvalidMultipartUpload):
		return http.StatusBadRequest, errInvalidMultipartUpload.Error()
	default:
		return http.StatusInternalServerError, "failed to store uploaded audio"
	}
}

func (s *service) readTranscriptionUpload(request *http.Request, task *ASRTask) (transcriptionUpload, error) {
	reader, err := request.MultipartReader()
	if err != nil {
		return transcriptionUpload{}, errors.Join(errInvalidMultipartUpload, err)
	}

	var upload transcriptionUpload
	complete := false
	defer func() {
		if !complete {
			removeTemporaryFile(upload.TempFilePath)
		}
	}()

	for {
		part, nextErr := reader.NextPart()
		if errors.Is(nextErr, io.EOF) {
			break
		}
		if nextErr != nil {
			return transcriptionUpload{}, errors.Join(errInvalidMultipartUpload, nextErr)
		}
		if err := s.fillTranscriptionUploadFromPart(&upload, task, part); err != nil {
			if closeErr := part.Close(); closeErr != nil {
				log.Printf("close multipart upload part after error: %v", closeErr)
			}
			return transcriptionUpload{}, err
		}
		if err := part.Close(); err != nil {
			return transcriptionUpload{}, errors.Join(errInvalidMultipartUpload, err)
		}
	}
	if upload.TempFilePath == "" {
		return transcriptionUpload{}, errAudioFileRequired
	}
	complete = true
	return upload, nil
}

func (s *service) fillTranscriptionUploadFromPart(upload *transcriptionUpload, task *ASRTask, part *multipart.Part) error {
	if part.FormName() == "file" && part.FileName() != "" {
		if upload.TempFilePath != "" {
			return nil
		}
		path, err := s.storeAudioPart(part, task)
		if err != nil {
			return err
		}
		upload.Filename = part.FileName()
		upload.TempFilePath = path
		return nil
	}

	switch part.FormName() {
	case "model", "level", "language", "response_format", "task_id":
	default:
		return nil
	}
	value, err := readTranscriptionFormField(part)
	if err != nil {
		return err
	}
	switch part.FormName() {
	case "model":
		upload.Model = value
	case "level":
		upload.Level = value
	case "language":
		upload.Language = value
	case "response_format":
		upload.ResponseFormat = value
	case "task_id":
		upload.TaskID = value
	}
	return nil
}

func readTranscriptionFormField(part io.Reader) (string, error) {
	value, err := io.ReadAll(io.LimitReader(part, maxTranscriptionFormFieldSize+1))
	if err != nil {
		return "", errors.Join(errInvalidMultipartUpload, err)
	}
	if len(value) > maxTranscriptionFormFieldSize {
		return "", errInvalidMultipartUpload
	}
	return string(value), nil
}

func (s *service) storeAudioPart(input io.Reader, task *ASRTask) (string, error) {
	tempFile, err := os.CreateTemp("", "asr-upload-*.tmp")
	if err != nil {
		return "", err
	}
	tempPath := tempFile.Name()
	complete := false
	closed := false
	defer func() {
		if !closed {
			if err := tempFile.Close(); err != nil {
				log.Printf("close temporary audio: %v", err)
			}
		}
		if !complete {
			removeTemporaryFile(tempPath)
		}
	}()

	if err := s.copyAudioWithStorageReservation(tempFile, input, task); err != nil {
		return "", err
	}
	closeErr := tempFile.Close()
	closed = true
	if closeErr != nil {
		return "", closeErr
	}
	complete = true
	return tempPath, nil
}

func (s *service) copyAudioWithStorageReservation(output io.Writer, input io.Reader, task *ASRTask) error {
	buffer := make([]byte, 32<<10)
	for {
		count, readErr := input.Read(buffer)
		if count > 0 {
			if task.TempFileSize+int64(count) > maxAudioFileSize {
				return &http.MaxBytesError{Limit: maxAudioFileSize}
			}
			if err := s.taskManager.growTaskStorage(task, int64(count)); err != nil {
				return err
			}
			written, err := output.Write(buffer[:count])
			if err != nil {
				return err
			}
			if written != count {
				return io.ErrShortWrite
			}
		}
		if errors.Is(readErr, io.EOF) {
			return nil
		}
		if readErr != nil {
			return errors.Join(errInvalidMultipartUpload, readErr)
		}
	}
}

func (s *service) cancelTask(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxCancelRequestSize)
	id := c.Param("id")
	if id == "" {
		id = c.Query("id")
	}
	if id == "" {
		id = c.PostForm("id")
	}
	if id == "" {
		var body struct {
			ID string `json:"id"`
		}
		if err := c.ShouldBindJSON(&body); err == nil {
			id = body.ID
		}
	}

	if id == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "task id is required"})
		return
	}
	if err := validateTaskID(id); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	found, err := s.taskManager.CancelAndWait(id, s.cancelTaskForModel, cancelTaskCleanupTimeout)
	if !found {
		c.JSON(http.StatusNotFound, gin.H{"error": fmt.Sprintf("task %s not found or already completed", id)})
		return
	}
	if err != nil {
		if errors.Is(err, errTaskCleanupTimeout) {
			c.JSON(http.StatusGatewayTimeout, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusBadGateway, gin.H{"error": "failed to confirm upstream cancellation"})
		return
	}

	c.JSON(http.StatusOK, gin.H{"status": "cancelled", "id": id})
}
