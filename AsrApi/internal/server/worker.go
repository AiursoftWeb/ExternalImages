package server

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"mime/multipart"
	"net/http"
	"os"
	"time"
)

func (s *service) startQueueWorkers(tm *TaskManager) {
	// 同一模型串行执行以避免争抢同一 GPU；不同模型使用独立 worker 以支持部署在不同设备。
	for model, queue := range tm.queues {
		log.Printf("[Queue] Starting task queue worker for model %s...", model)
		go func(queue *taskQueue) {
			for {
				task := tm.waitForPendingTask(queue)
				log.Printf("[Queue] Worker picked up task %s. Filename: %s", task.ID, task.Filename)

				result := s.processTask(task)
				task.CancelFunc()

				publishResult := tm.finishTask(task, result)
				tm.completeTaskCleanup(task)

				if !publishResult {
					continue
				}
				publishTaskResult(task, result)
			}
		}(queue)
	}
}

func (tm *TaskManager) finishTask(task *ASRTask, result ASRTaskResult) bool {
	tm.mu.Lock()
	defer tm.mu.Unlock()
	if task.Status != StatusRunning {
		log.Printf("[Queue] Worker finished task %s. Current status: %s", task.ID, task.Status)
		return false
	}
	if result.Err != nil || result.StatusCode < http.StatusOK || result.StatusCode >= http.StatusMultipleChoices {
		task.Status = StatusFailed
		if result.Err != nil {
			log.Printf("[Queue] Worker finished task %s. Result: Failure (%v)", task.ID, result.Err)
		} else {
			log.Printf("[Queue] Worker finished task %s. Result: Failure (upstream status %d)", task.ID, result.StatusCode)
		}
	} else {
		task.Status = StatusCompleted
		log.Printf("[Queue] Worker finished task %s. Result: Success", task.ID)
	}
	if currentTask := tm.tasks[task.ID]; currentTask == task {
		delete(tm.tasks, task.ID)
	}
	return true
}

func (tm *TaskManager) waitForPendingTask(queue *taskQueue) *ASRTask {
	for {
		tm.mu.Lock()
		if len(queue.pending) > 0 {
			task := queue.pending[0]
			queue.pending[0] = nil
			queue.pending = queue.pending[1:]
			task.Status = StatusRunning
			tm.mu.Unlock()
			return task
		}
		tm.mu.Unlock()
		<-queue.notify
	}
}

func removeTemporaryFile(path string) {
	if path == "" {
		return
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		log.Printf("remove temporary file %s: %v", path, err)
	}
}

func (s *service) processTask(task *ASRTask) ASRTaskResult {
	if err := task.Ctx.Err(); err != nil {
		return cancelledTaskResult()
	}

	if !s.acquireTranscriptionSlot(task.Ctx) {
		return cancelledTaskResult()
	}
	defer s.releaseTranscriptionSlot()

	backend, ok := s.upstreams[task.Model]
	if !ok {
		return ASRTaskResult{
			StatusCode: http.StatusBadRequest,
			Body:       []byte(fmt.Sprintf(`{"error":"model %s is not supported"}`, task.Model)),
			Err:        fmt.Errorf("model %s not supported", task.Model),
		}
	}

	executionContext, cancelExecution := context.WithTimeout(task.Ctx, s.configuredTranscriptionTimeout())
	defer cancelExecution()

	modelForUpstream := backend.model
	if task.Level != "" {
		modelForUpstream = task.Level
	}

	file, err := os.Open(task.TempFilePath)
	if err != nil {
		return ASRTaskResult{
			StatusCode: http.StatusInternalServerError,
			Body:       []byte(`{"error":"failed to open temporary audio file"}`),
			Err:        err,
		}
	}
	defer file.Close()

	body, contentType := buildUpstreamBody(file, task.Filename, modelForUpstream, task.Language, task.ResponseFormat)

	request, err := http.NewRequestWithContext(executionContext, http.MethodPost, backend.url+"/v1/audio/transcriptions", body)
	if err != nil {
		return ASRTaskResult{
			StatusCode: http.StatusInternalServerError,
			Body:       []byte(`{"error":"failed to create upstream request"}`),
			Err:        err,
		}
	}
	request.Header.Set("Content-Type", contentType)
	request.Header.Set("Authorization", "Bearer "+backend.token)
	request.Header.Set("X-Task-Id", task.ID)

	log.Printf("[Queue] Sending HTTP post request to %s upstream for task %s", task.Model, task.ID)
	response, err := s.client.Do(request)
	if err != nil {
		if errors.Is(executionContext.Err(), context.DeadlineExceeded) {
			s.cancelUpstreamAfterRequestFailure(task, backend)
			return ASRTaskResult{
				StatusCode: http.StatusGatewayTimeout,
				Body:       []byte(`{"error":"transcription execution timed out"}`),
				Err:        context.DeadlineExceeded,
			}
		}
		if task.Ctx.Err() == nil {
			s.cancelUpstreamAfterRequestFailure(task, backend)
		}
		return ASRTaskResult{
			StatusCode: http.StatusBadGateway,
			Body:       []byte(`{"error":"model service is unavailable or request was cancelled"}`),
			Err:        err,
		}
	}
	defer response.Body.Close()

	respBody, err := io.ReadAll(response.Body)
	if err != nil {
		return ASRTaskResult{
			StatusCode: http.StatusInternalServerError,
			Body:       []byte(`{"error":"failed to read upstream response"}`),
			Err:        err,
		}
	}

	return ASRTaskResult{
		StatusCode: response.StatusCode,
		Body:       respBody,
		Header:     response.Header,
	}
}

func (s *service) configuredSegmentDuration() time.Duration {
	if s.segmentDuration > 0 {
		return s.segmentDuration
	}
	return defaultSegmentDurationSeconds * time.Second
}

func (s *service) configuredSegmentOverlap() time.Duration {
	if s.segmentOverlap > 0 {
		return s.segmentOverlap
	}
	return defaultSegmentOverlapSeconds * time.Second
}

func (s *service) configuredTranscriptionTimeout() time.Duration {
	if s.transcriptionTimeout > 0 {
		return s.transcriptionTimeout
	}
	return defaultTranscriptionTimeoutSeconds * time.Second
}

func (s *service) cancelUpstreamAfterRequestFailure(task *ASRTask, backend upstream) {
	cancelUpstreamFunc := func() error {
		return s.cancelUpstream(backend, task.ID)
	}
	var cancelErr error
	if s.taskManager == nil {
		cancelErr = cancelUpstreamFunc()
	} else {
		cancelErr = s.taskManager.runCompensatingCancellation(task.ID, task.Model, cancelUpstreamFunc)
	}
	if cancelErr != nil {
		log.Printf("[Queue] Failed to stop upstream task %s after transcription request failure: %v", task.ID, cancelErr)
	}
}

func (s *service) acquireTranscriptionSlot(ctx context.Context) bool {
	if s.transcribeSem == nil {
		return true
	}
	select {
	case s.transcribeSem <- struct{}{}:
		return true
	case <-ctx.Done():
		return false
	}
}

func (s *service) releaseTranscriptionSlot() {
	if s.transcribeSem != nil {
		<-s.transcribeSem
	}
}

func (s *service) cancelTaskForModel(model, taskID string) error {
	if model != "whisperx" && model != "funasr" {
		return fmt.Errorf("model %s does not support task cancellation", model)
	}
	backend, ok := s.upstreams[model]
	if !ok {
		return fmt.Errorf("model %s is not configured", model)
	}
	return s.cancelUpstream(backend, taskID)
}

func (s *service) cancelUpstream(backend upstream, taskID string) error {
	ctx, cancel := context.WithTimeout(context.Background(), cancelUpstreamTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, backend.url+"/v1/cancel", nil)
	if err != nil {
		return fmt.Errorf("create cancel request for upstream %s: %w", backend.url, err)
	}
	req.Header.Set("Authorization", "Bearer "+backend.token)
	req.Header.Set("X-Task-Id", taskID)
	resp, err := s.statusClient.Do(req)
	if err != nil {
		return fmt.Errorf("send cancel request to upstream %s: %w", backend.url, err)
	}
	defer func() {
		if err := resp.Body.Close(); err != nil {
			log.Printf("[Queue] Failed to close cancel response from upstream %s: %v", backend.url, err)
		}
	}()
	if _, err := io.Copy(io.Discard, resp.Body); err != nil {
		return fmt.Errorf("read cancel response from upstream %s: %w", backend.url, err)
	}
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusAccepted {
		return fmt.Errorf("upstream %s returned cancellation status %d", backend.url, resp.StatusCode)
	}
	log.Printf("[Queue] Upstream %s accepted cancellation for task %s with status %d", backend.url, taskID, resp.StatusCode)
	return nil
}

func buildUpstreamBody(input io.Reader, filename, model, language, responseFormat string) (io.Reader, string) {
	reader, writer := io.Pipe()
	multipartWriter := multipart.NewWriter(writer)
	go func() {
		if err := writeUpstreamBody(multipartWriter, input, filename, model, language, responseFormat); err != nil {
			if closeErr := writer.CloseWithError(err); closeErr != nil {
				log.Printf("close upstream request body after error: %v", closeErr)
			}
			return
		}
		if err := writer.Close(); err != nil {
			log.Printf("close upstream request body: %v", err)
		}
	}()
	return reader, multipartWriter.FormDataContentType()
}

func writeUpstreamBody(writer *multipart.Writer, input io.Reader, filename, model, language, responseFormat string) error {
	if err := writer.WriteField("model", model); err != nil {
		return err
	}
	if language != "" {
		if err := writer.WriteField("language", language); err != nil {
			return err
		}
	}
	if responseFormat != "" {
		if err := writer.WriteField("response_format", responseFormat); err != nil {
			return err
		}
	}
	part, err := writer.CreateFormFile("file", filename)
	if err != nil {
		return err
	}
	if _, err := io.Copy(part, input); err != nil {
		return err
	}
	return writer.Close()
}
