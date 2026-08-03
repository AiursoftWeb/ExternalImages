package server

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"sync"
	"time"
)

const (
	maxAudioFileSize                   = 1 << 30
	maxUploadRequestOverhead           = 1 << 20
	maxTranscriptionFormFieldSize      = 4 << 10
	maxCancelRequestSize               = 4 << 10
	maxTaskIDLength                    = 128
	defaultMaxStoredAudioSizeMiB       = 2048
	maxConfiguredStoredAudioMiB        = 1 << 20
	maxConfiguredConcurrency           = 1024
	defaultTranscriptionTimeoutSeconds = 30 * 60
	maxConfiguredDurationSeconds       = 24 * 60 * 60
	maxCancelTombstones                = 1024
	cancelTombstoneTTL                 = 10 * time.Minute
	cancelUpstreamTimeout              = 12 * time.Second
	cancelTaskCleanupTimeout           = 12 * time.Second
)

var (
	errStoredAudioCapacity = errors.New("stored audio capacity is full, please try again later")
	errTaskCleanupTimeout  = errors.New("timed out waiting for cancelled task cleanup")
)

type upstream struct {
	url   string
	model string
	token string
}

type TaskStatus string

const (
	StatusPending      TaskStatus = "pending"
	StatusRunning      TaskStatus = "running"
	StatusCancelling   TaskStatus = "cancelling"
	StatusCancelFailed TaskStatus = "cancel_failed"
	StatusCompleted    TaskStatus = "completed"
	StatusFailed       TaskStatus = "failed"
	StatusCancelled    TaskStatus = "cancelled"
)

type ASRTask struct {
	ID        string     `json:"id"`
	Status    TaskStatus `json:"status"`
	Model     string     `json:"model"`
	Level     string     `json:"level"`
	Language  string     `json:"language"`
	Filename  string     `json:"filename"`
	CreatedAt time.Time  `json:"created_at"`

	TempFilePath    string             `json:"-"`
	TempFileSize    int64              `json:"-"`
	ResponseFormat  string             `json:"-"`
	ResultChan      chan ASRTaskResult `json:"-"`
	Ctx             context.Context    `json:"-"`
	CancelFunc      context.CancelFunc `json:"-"`
	storageReserved bool
	cleanupDone     chan struct{}
	cleanupOnce     sync.Once
}

type ASRTaskResult struct {
	StatusCode int
	Body       []byte
	Header     http.Header
	Err        error
}

type cancellationRecord struct {
	Model     string
	ExpiresAt time.Time
	Retryable bool
}

type cancellationAttempt struct {
	done chan struct{}
	err  error
}

type TaskManager struct {
	mu                   sync.RWMutex
	tasks                map[string]*ASRTask
	queues               map[string]*taskQueue
	cancelledTaskIDs     map[string]cancellationRecord
	cancellationAttempts map[string]*cancellationAttempt
	maxStoredBytes       int64
	storedBytes          int64
}

type taskQueue struct {
	pending  []*ASRTask
	capacity int
	notify   chan struct{}
}

func NewTaskManager(queueSize int, maxStoredBytes int64, models map[string]upstream) *TaskManager {
	manager := &TaskManager{
		tasks:                make(map[string]*ASRTask),
		queues:               make(map[string]*taskQueue, len(models)),
		cancelledTaskIDs:     make(map[string]cancellationRecord),
		cancellationAttempts: make(map[string]*cancellationAttempt),
		maxStoredBytes:       maxStoredBytes,
	}
	for model := range models {
		manager.queues[model] = &taskQueue{
			pending:  make([]*ASRTask, 0, queueSize),
			capacity: queueSize,
			notify:   make(chan struct{}, 1),
		}
	}
	return manager
}

func (tm *TaskManager) Add(task *ASRTask) error {
	tm.mu.Lock()
	defer tm.mu.Unlock()
	if task.cleanupDone == nil {
		task.cleanupDone = make(chan struct{})
	}
	tm.pruneCancelledTaskIDs(time.Now())
	if _, exists := tm.cancelledTaskIDs[task.ID]; exists {
		return fmt.Errorf("task %s was recently cancelled", task.ID)
	}
	if _, exists := tm.tasks[task.ID]; exists {
		return fmt.Errorf("task %s already exists", task.ID)
	}
	queue, exists := tm.queues[task.Model]
	if !exists {
		return fmt.Errorf("model %s does not have a task queue", task.Model)
	}
	if len(queue.pending) >= queue.capacity {
		return fmt.Errorf("task queue is full, please try again later")
	}
	if !task.storageReserved {
		if err := tm.reserveTaskStorageLocked(task, task.TempFileSize); err != nil {
			return err
		}
	}
	tm.tasks[task.ID] = task
	queue.pending = append(queue.pending, task)
	select {
	case queue.notify <- struct{}{}:
	default:
	}
	log.Printf("[Queue] Task %s added to queue. Filename: %s, Model: %s, Level: %s (Queue size: %d)", task.ID, task.Filename, task.Model, task.Level, len(queue.pending))
	return nil
}

func (tm *TaskManager) Cancel(id string, cancelUpstreamFunc func(model, taskID string) error) (bool, error) {
	tm.mu.Lock()
	tm.pruneCancelledTaskIDs(time.Now())
	if attempt, exists := tm.cancellationAttempts[id]; exists {
		tm.mu.Unlock()
		return waitForCancellationAttempt(attempt)
	}
	if record, exists := tm.cancelledTaskIDs[id]; exists {
		if record.Retryable {
			attempt := tm.startCancellationAttempt(id)
			tm.mu.Unlock()
			return tm.retryCancellation(id, record, attempt, cancelUpstreamFunc)
		}
		tm.mu.Unlock()
		return false, nil
	}
	task, ok := tm.tasks[id]
	if !ok {
		tm.mu.Unlock()
		return false, nil
	}
	if task.Status == StatusCompleted || task.Status == StatusFailed || task.Status == StatusCancelled {
		tm.mu.Unlock()
		return false, nil
	}
	prevStatus := task.Status
	if prevStatus == StatusPending {
		task.Status = StatusCancelled
		tm.removePendingTask(task)
		delete(tm.tasks, id)
		tm.recordCancelledTaskID(id, task.Model, false, time.Now())
		if task.CancelFunc != nil {
			task.CancelFunc()
		}
		tm.mu.Unlock()
		log.Printf("[Queue] Task %s is cancelled. Previous status: %s", id, prevStatus)
		tm.completeTaskCleanup(task)
		publishTaskResult(task, cancelledTaskResult())
		return true, nil
	}

	attempt := tm.startCancellationAttempt(id)
	task.Status = StatusCancelling
	if task.CancelFunc != nil {
		task.CancelFunc()
	}
	tm.mu.Unlock()

	var cancelErr error
	if cancelUpstreamFunc != nil {
		cancelErr = cancelUpstreamFunc(task.Model, task.ID)
	}

	tm.mu.Lock()
	if currentTask := tm.tasks[id]; currentTask == task {
		if cancelErr == nil {
			task.Status = StatusCancelled
		} else {
			task.Status = StatusCancelFailed
		}
		delete(tm.tasks, id)
		tm.recordCancelledTaskID(id, task.Model, cancelErr != nil, time.Now())
	}
	tm.finishCancellationAttempt(id, attempt, cancelErr)
	tm.mu.Unlock()

	if cancelErr != nil {
		log.Printf("[Queue] Failed to confirm cancellation for task %s: %v", id, cancelErr)
		publishTaskResult(task, ASRTaskResult{
			StatusCode: http.StatusBadGateway,
			Body:       []byte(`{"error":"failed to confirm upstream cancellation"}`),
			Err:        cancelErr,
		})
		return true, cancelErr
	}

	log.Printf("[Queue] Task %s is cancelled. Previous status: %s", id, prevStatus)
	publishTaskResult(task, cancelledTaskResult())
	return true, nil
}

func (tm *TaskManager) CancelAndWait(id string, cancelUpstreamFunc func(model, taskID string) error, cleanupTimeout time.Duration) (bool, error) {
	tm.mu.RLock()
	task := tm.tasks[id]
	tm.mu.RUnlock()

	found, err := tm.Cancel(id, cancelUpstreamFunc)
	if !found || err != nil || task == nil {
		return found, err
	}

	timer := time.NewTimer(cleanupTimeout)
	defer timer.Stop()
	select {
	case <-task.cleanupDone:
		return true, nil
	case <-timer.C:
		return true, errTaskCleanupTimeout
	}
}

func (tm *TaskManager) CancelAndWaitOrReserve(id string, cancelUpstreamFunc func(model, taskID string) error, cleanupTimeout time.Duration) (bool, error) {
	for {
		found, err := tm.CancelAndWait(id, cancelUpstreamFunc, cleanupTimeout)
		if found || err != nil {
			return found, err
		}

		tm.mu.Lock()
		tm.pruneCancelledTaskIDs(time.Now())
		if _, exists := tm.tasks[id]; exists {
			tm.mu.Unlock()
			continue
		}
		tm.recordCancelledTaskID(id, "", false, time.Now())
		tm.mu.Unlock()
		return true, nil
	}
}

func (tm *TaskManager) retryCancellation(id string, record cancellationRecord, attempt *cancellationAttempt, cancelUpstreamFunc func(model, taskID string) error) (bool, error) {
	var cancelErr error
	if cancelUpstreamFunc != nil {
		cancelErr = cancelUpstreamFunc(record.Model, id)
	}

	tm.mu.Lock()
	if currentRecord, exists := tm.cancelledTaskIDs[id]; exists {
		currentRecord.ExpiresAt = time.Now().Add(cancelTombstoneTTL)
		if cancelErr == nil {
			currentRecord.Retryable = false
		}
		tm.cancelledTaskIDs[id] = currentRecord
	}
	tm.finishCancellationAttempt(id, attempt, cancelErr)
	tm.mu.Unlock()

	if cancelErr != nil {
		log.Printf("[Queue] Failed to confirm cancellation retry for task %s: %v", id, cancelErr)
		return true, cancelErr
	}
	log.Printf("[Queue] Upstream cancellation retry succeeded for task %s", id)
	return true, nil
}

func (tm *TaskManager) startCancellationAttempt(id string) *cancellationAttempt {
	attempt := &cancellationAttempt{done: make(chan struct{})}
	tm.cancellationAttempts[id] = attempt
	return attempt
}

func (tm *TaskManager) finishCancellationAttempt(id string, attempt *cancellationAttempt, err error) {
	attempt.err = err
	close(attempt.done)
	delete(tm.cancellationAttempts, id)
}

func waitForCancellationAttempt(attempt *cancellationAttempt) (bool, error) {
	<-attempt.done
	return true, attempt.err
}

func (tm *TaskManager) runCompensatingCancellation(id, model string, cancelUpstreamFunc func() error) error {
	tm.mu.Lock()
	tm.pruneCancelledTaskIDs(time.Now())
	if attempt, exists := tm.cancellationAttempts[id]; exists {
		tm.mu.Unlock()
		_, err := waitForCancellationAttempt(attempt)
		return err
	}
	attempt := tm.startCancellationAttempt(id)
	tm.mu.Unlock()

	cancelErr := cancelUpstreamFunc()

	tm.mu.Lock()
	tm.recordCancelledTaskID(id, model, cancelErr != nil, time.Now())
	tm.finishCancellationAttempt(id, attempt, cancelErr)
	tm.mu.Unlock()
	return cancelErr
}

func (tm *TaskManager) reserveTaskStorage(task *ASRTask, size int64) error {
	tm.mu.Lock()
	defer tm.mu.Unlock()
	return tm.reserveTaskStorageLocked(task, size)
}

func (tm *TaskManager) reserveTaskStorageLocked(task *ASRTask, size int64) error {
	if size < 0 {
		return errors.New("stored audio size must not be negative")
	}
	if size > tm.maxStoredBytes-tm.storedBytes {
		return errStoredAudioCapacity
	}
	tm.storedBytes += size
	task.TempFileSize = size
	task.storageReserved = true
	return nil
}

func (tm *TaskManager) growTaskStorage(task *ASRTask, additionalBytes int64) error {
	tm.mu.Lock()
	defer tm.mu.Unlock()
	if additionalBytes < 0 {
		return errors.New("additional stored audio size must not be negative")
	}
	if additionalBytes > tm.maxStoredBytes-tm.storedBytes {
		return errStoredAudioCapacity
	}
	tm.storedBytes += additionalBytes
	task.TempFileSize += additionalBytes
	task.storageReserved = true
	return nil
}

func (tm *TaskManager) releaseTaskStorage(task *ASRTask) {
	tm.mu.Lock()
	defer tm.mu.Unlock()
	if !task.storageReserved {
		return
	}
	tm.storedBytes -= task.TempFileSize
	task.storageReserved = false
}

func (tm *TaskManager) completeTaskCleanup(task *ASRTask) {
	removeTemporaryFile(task.TempFilePath)
	tm.releaseTaskStorage(task)
	task.cleanupOnce.Do(func() {
		close(task.cleanupDone)
	})
}

func (tm *TaskManager) recordCancelledTaskID(id, model string, retryable bool, now time.Time) {
	tm.pruneCancelledTaskIDs(now)
	if currentRecord, exists := tm.cancelledTaskIDs[id]; exists && !currentRecord.Retryable {
		retryable = false
	}
	tm.cancelledTaskIDs[id] = cancellationRecord{
		Model:     model,
		ExpiresAt: now.Add(cancelTombstoneTTL),
		Retryable: retryable,
	}
	if len(tm.cancelledTaskIDs) > maxCancelTombstones {
		oldestID := id
		oldestExpiry := tm.cancelledTaskIDs[id].ExpiresAt
		for cancelledID, record := range tm.cancelledTaskIDs {
			if record.ExpiresAt.Before(oldestExpiry) {
				oldestID = cancelledID
				oldestExpiry = record.ExpiresAt
			}
		}
		delete(tm.cancelledTaskIDs, oldestID)
	}
}

func (tm *TaskManager) pruneCancelledTaskIDs(now time.Time) {
	for id, record := range tm.cancelledTaskIDs {
		if !record.ExpiresAt.After(now) {
			delete(tm.cancelledTaskIDs, id)
		}
	}
}

func cancelledTaskResult() ASRTaskResult {
	return ASRTaskResult{
		StatusCode: http.StatusBadRequest,
		Body:       []byte(`{"error":"Task was cancelled"}`),
		Err:        errors.New("task was cancelled"),
	}
}

func publishTaskResult(task *ASRTask, result ASRTaskResult) {
	select {
	case task.ResultChan <- result:
	default:
		log.Printf("[Queue] Task %s result channel is full (handler probably returned already)", task.ID)
	}
}

func (tm *TaskManager) removePendingTask(task *ASRTask) {
	queue := tm.queues[task.Model]
	for index, pendingTask := range queue.pending {
		if pendingTask.ID != task.ID {
			continue
		}
		copy(queue.pending[index:], queue.pending[index+1:])
		lastIndex := len(queue.pending) - 1
		queue.pending[lastIndex] = nil
		queue.pending = queue.pending[:lastIndex]
		return
	}
}
