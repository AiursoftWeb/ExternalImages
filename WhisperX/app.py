import atexit
import multiprocessing
import os
import queue
import secrets
import shutil
import tempfile
import threading
import time

import whisperx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from models_config import BAKED_MODELS, ensure_baked_model

app = FastAPI()
model_token = os.getenv("ASR_WHISPERX_TOKEN", "").strip()
if not model_token:
    raise RuntimeError("ASR_WHISPERX_TOKEN is required")

MODEL_REGISTRY = {}
MODEL_LOCK = threading.Lock()
CANCEL_TOMBSTONE_TTL = 10 * 60
MAX_CANCEL_TOMBSTONES = 1024


class TranscriptionCancelled(Exception):
    pass


class TranscriptionBusy(Exception):
    pass


def load_model(name: str):
    """Lazily load a whisperx model and cache it. Unknown or unloadable names
    raise so the caller can return a clear error instead of a 500."""
    ensure_baked_model(name)

    with MODEL_LOCK:
        if name in MODEL_REGISTRY:
            return MODEL_REGISTRY[name]

        # Clear previous models to release VRAM/RAM before loading a new one
        MODEL_REGISTRY.clear()
        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        try:
            model = whisperx.load_model(
                name,
                os.getenv("WHISPERX_DEVICE", "cuda"),
                compute_type=os.getenv("WHISPERX_COMPUTE_TYPE", "float16"),
            )
        except Exception as exc:
            raise ValueError(f"cannot load model '{name}': {exc}") from exc
        MODEL_REGISTRY[name] = model
        return model


def inference_worker(command_queue, result_queue):
    while True:
        command = command_queue.get()
        if command is None:
            return

        task_id = command["task_id"]
        try:
            model = load_model(command["model"])
            result_queue.put({
                "task_id": task_id,
                "status": "model_loaded",
                "model": command["model"],
            })
            result = model.transcribe(
                whisperx.load_audio(command["audio_path"]),
                language=command["language"] or None,
            )
            text = " ".join(segment["text"].strip() for segment in result["segments"])
            payload = {"text": text}
            if command["response_format"] != "json":
                payload.update({
                    "language": result.get("language"),
                    "segments": result["segments"],
                })
            result_queue.put({
                "task_id": task_id,
                "status": "completed",
                "model": command["model"],
                "payload": payload,
            })
        except ValueError as exc:
            result_queue.put({
                "task_id": task_id,
                "status": "invalid_model",
                "error": str(exc),
            })
        except Exception as exc:
            result_queue.put({
                "task_id": task_id,
                "status": "failed",
                "error": str(exc),
            })


class InferenceProcess:
    def __init__(self):
        self.context = multiprocessing.get_context("spawn")
        self.state_lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.process = None
        self.command_queue = None
        self.result_queue = None
        self.generation = 0
        self.active_task_id = None
        self.active_task_done = None
        self.loaded_model = None
        self.cancelled_task_ids = {}

    def transcribe(self, task_id, audio_path, model, language, response_format):
        if not self.run_lock.acquire(blocking=False):
            raise TranscriptionBusy("another transcription is already running")

        task_done = None
        command_queue = None
        result_queue = None
        generation = None
        cancelled = False
        try:
            task_id, generation, command_queue, result_queue, task_done = self._prepare_task(task_id)
            command_queue.put({
                "task_id": task_id,
                "audio_path": audio_path,
                "model": model,
                "language": language,
                "response_format": response_format,
            })
            result = self._wait_for_result(task_id, generation, result_queue)
        finally:
            if generation is not None:
                with self.state_lock:
                    cancelled = self.generation != generation
                    if self.generation == generation and self.active_task_id == task_id:
                        self.active_task_id = None
                        self.active_task_done = None
                if cancelled:
                    self._close_queue(command_queue)
                    self._close_queue(result_queue)
                task_done.set()
            self.run_lock.release()

        if cancelled:
            raise TranscriptionCancelled("transcription was cancelled")
        return result

    def cancel(self, task_id):
        task_done = None
        with self.state_lock:
            self._record_cancelled_task(task_id)
            if self.active_task_id is None or self.process is None:
                return "accepted"
            if not secrets.compare_digest(self.active_task_id, task_id):
                return "accepted"
            process = self.process
            task_done = self.active_task_done
            self.process = None
            self.command_queue = None
            self.result_queue = None
            self.active_task_id = None
            self.active_task_done = None
            self.loaded_model = None
            self.generation += 1
            self._terminate_process(process)
        if task_done is not None and not task_done.wait(timeout=10):
            raise RuntimeError("timed out waiting for cancelled transcription cleanup")
        return "cancelled"

    def stop(self):
        with self.state_lock:
            process = self.process
            command_queue = self.command_queue
            result_queue = self.result_queue
            self.process = None
            self.command_queue = None
            self.result_queue = None
            self.active_task_id = None
            self.active_task_done = None
            self.loaded_model = None
            self.generation += 1
            if process is not None:
                self._terminate_process(process)
            self._close_queue(command_queue)
            self._close_queue(result_queue)

    def loaded_models(self):
        with self.state_lock:
            if self.process is None or not self.process.is_alive() or self.loaded_model is None:
                return []
            return [self.loaded_model]

    def _prepare_task(self, task_id=None):
        with self.state_lock:
            task_id = task_id or secrets.token_hex(16)
            self._prune_cancelled_tasks()
            if task_id in self.cancelled_task_ids:
                raise TranscriptionCancelled("transcription was cancelled before execution")
            if self.process is None or not self.process.is_alive():
                self._start_process()
            self.active_task_id = task_id
            self.active_task_done = threading.Event()
            return task_id, self.generation, self.command_queue, self.result_queue, self.active_task_done

    def _record_cancelled_task(self, task_id):
        self._prune_cancelled_tasks()
        self.cancelled_task_ids[task_id] = time.monotonic() + CANCEL_TOMBSTONE_TTL
        while len(self.cancelled_task_ids) > MAX_CANCEL_TOMBSTONES:
            oldest_task_id = min(self.cancelled_task_ids, key=self.cancelled_task_ids.get)
            del self.cancelled_task_ids[oldest_task_id]

    def _prune_cancelled_tasks(self):
        now = time.monotonic()
        expired_task_ids = [
            task_id
            for task_id, expires_at in self.cancelled_task_ids.items()
            if expires_at <= now
        ]
        for task_id in expired_task_ids:
            del self.cancelled_task_ids[task_id]

    def _start_process(self):
        self.command_queue = self.context.Queue()
        self.result_queue = self.context.Queue()
        self.process = self.context.Process(
            target=inference_worker,
            args=(self.command_queue, self.result_queue),
            daemon=True,
        )
        self.process.start()
        self.generation += 1
        self.loaded_model = None

    def _wait_for_result(self, task_id, generation, result_queue):
        while True:
            try:
                result = result_queue.get(timeout=0.2)
            except queue.Empty:
                with self.state_lock:
                    if self.generation != generation:
                        raise TranscriptionCancelled("transcription was cancelled")
                    if self.process is None or not self.process.is_alive():
                        raise RuntimeError("inference process exited unexpectedly")
                continue

            if result["task_id"] != task_id:
                continue
            with self.state_lock:
                if self.generation != generation:
                    raise TranscriptionCancelled("transcription was cancelled")
                if result["status"] == "model_loaded":
                    self.loaded_model = result["model"]
                    continue
                if result["status"] == "invalid_model":
                    raise ValueError(result["error"])
                if result["status"] == "failed":
                    raise RuntimeError(result["error"])
                self.loaded_model = result["model"]
                return result["payload"]

    @staticmethod
    def _terminate_process(process):
        if not process.is_alive():
            process.join()
            return
        process.terminate()
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join()

    @staticmethod
    def _close_queue(process_queue):
        if process_queue is None:
            return
        try:
            process_queue.close()
            process_queue.join_thread()
        except (OSError, ValueError) as exc:
            print(f"[WhisperX] Failed to close inference queue: {exc}")


inference_process = InferenceProcess()
atexit.register(inference_process.stop)


@app.middleware("http")
async def require_model_token(request: Request, call_next):
    if request.url.path == "/health" or request.url.path.startswith("/v1/"):
        expected = f"Bearer {model_token}"
        authorization = request.headers.get("Authorization", "")
        if not secrets.compare_digest(authorization, expected):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "models_loaded": inference_process.loaded_models()}


@app.get("/v1/models")
def list_models():
    loaded = set(inference_process.loaded_models())
    data = []
    for name in BAKED_MODELS:
        data.append({
            "id": name,
            "object": "model",
            "owned_by": "whisperx",
            "baked": True,
            "loaded": name in loaded,
        })
    return {"object": "list", "data": data}


@app.post("/v1/cancel")
def cancel(x_task_id: str = Header(default="", alias="X-Task-Id")):
    if not x_task_id:
        raise HTTPException(status_code=400, detail="task ID is required")
    status = inference_process.cancel(x_task_id)
    status_code = 200 if status == "cancelled" else 202
    return JSONResponse(
        status_code=status_code,
        content={"status": status, "id": x_task_id},
    )


@app.post("/v1/audio/transcriptions")
def transcribe(
    file: UploadFile = File(...),
    model: str = Form(default=""),
    language: str = Form(default=""),
    response_format: str = Form(default="json"),
    x_task_id: str = Header(default="", alias="X-Task-Id"),
):
    model_name = model or os.getenv("WHISPERX_MODEL", "large-v3")
    try:
        ensure_baked_model(model_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    print(f"[WhisperX] Received transcription request. Model: {model_name}, File: {file.filename}, Language: {language or 'auto'}")
    start_time = time.time()

    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(file.filename or "audio")[1]) as audio_file:
        shutil.copyfileobj(file.file, audio_file)
        audio_file.flush()
        print(f"[WhisperX] Saved temp audio file: {audio_file.name}. Starting transcription...")
        try:
            result = inference_process.transcribe(
                x_task_id,
                audio_file.name,
                model_name,
                language,
                response_format,
            )
        except TranscriptionBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TranscriptionCancelled as exc:
            raise HTTPException(status_code=499, detail=str(exc)) from exc
        except ValueError as exc:
            print(f"[WhisperX] Error loading model {model_name}: {exc}")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    elapsed = time.time() - start_time
    print(f"[WhisperX] Transcription completed for {file.filename} in {elapsed:.2f} seconds")
    return result
