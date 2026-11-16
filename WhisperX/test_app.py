import importlib
import asyncio
import queue
from unittest.mock import Mock, patch
import io
import os
import sys
import threading
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class FakeFastAPI:
    def __init__(self, **kwargs):
        self.lifespan = kwargs.get("lifespan")

    def middleware(self, _):
        return self.route

    def get(self, _):
        return self.route

    def post(self, _):
        return self.route

    @staticmethod
    def route(function):
        return function


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class FakeJSONResponse:
    def __init__(self, status_code=200, content=None):
        self.status_code = status_code
        self.content = content


def parameter(default=None, **_):
    return default


class FakeWhisperModel:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, audio_path, language=None, **kwargs):
        fake_segment = types.SimpleNamespace(start=0.0, end=1.5, text="Hello world")
        fake_info = types.SimpleNamespace(language="en")
        return [fake_segment], fake_info


def load_app_module():
    module_names = ["faster_whisper", "fastapi", "fastapi.responses", "models_config"]
    original_modules = {name: sys.modules.get(name) for name in module_names}
    original_token = os.environ.get("ASR_WHISPERX_TOKEN")
    faster_whisper = types.ModuleType("faster_whisper")
    faster_whisper.WhisperModel = FakeWhisperModel
    fastapi = types.ModuleType("fastapi")
    fastapi.BackgroundTasks = object
    fastapi.FastAPI = FakeFastAPI
    fastapi.File = parameter
    fastapi.Form = parameter
    fastapi.Header = parameter
    fastapi.HTTPException = FakeHTTPException
    fastapi.Request = object
    fastapi.UploadFile = object
    responses = types.ModuleType("fastapi.responses")
    responses.JSONResponse = FakeJSONResponse
    models_config = types.ModuleType("models_config")
    models_config.BAKED_MODELS = ["large-v3"]
    models_config.ensure_baked_model = lambda _: None

    try:
        sys.modules["faster_whisper"] = faster_whisper
        sys.modules["fastapi"] = fastapi
        sys.modules["fastapi.responses"] = responses
        sys.modules["models_config"] = models_config
        os.environ["ASR_WHISPERX_TOKEN"] = "test-token"
        sys.modules.pop("app", None)
        return importlib.import_module("app")
    finally:
        for name, module in original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if original_token is None:
            os.environ.pop("ASR_WHISPERX_TOKEN", None)
        else:
            os.environ["ASR_WHISPERX_TOKEN"] = original_token


class InferenceProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def test_startup_preloads_before_serving_and_stops_on_shutdown(self):
        process = Mock()
        events = []
        process.preload.side_effect = lambda: events.append("loaded")
        process.stop.side_effect = lambda: events.append("stopped")

        async def run():
            async with self.app.lifespan(None):
                self.assertEqual(events, ["loaded"])
                events.append("serving")

        with patch.object(self.app, "PRELOAD_ENABLED", True), patch.object(self.app, "inference_process", process):
            asyncio.run(run())
        self.assertEqual(events, ["loaded", "serving", "stopped"])

    def test_failed_preload_prevents_startup_and_cleans_worker(self):
        process = Mock()
        process.preload.side_effect = RuntimeError("CUDA out of memory")

        async def run():
            async with self.app.lifespan(None):
                self.fail("Failed preload must not begin serving")

        with patch.object(self.app, "PRELOAD_ENABLED", True), patch.object(self.app, "inference_process", process):
            with self.assertRaisesRegex(RuntimeError, "out of memory"):
                asyncio.run(run())
        process.stop.assert_called_once()

    def test_preload_worker_loads_without_an_audio_request(self):
        commands, results = queue.Queue(), queue.Queue()
        commands.put({"task_id": "startup", "operation": "preload", "model": "large-v3"})
        commands.put(None)
        model = Mock()
        with patch.object(self.app, "load_model", return_value=model) as load:
            self.app.inference_worker(commands, results)
        load.assert_called_once_with("large-v3")
        model.transcribe.assert_not_called()
        self.assertEqual(results.get()["status"], "model_loaded")
        self.assertEqual(results.get()["status"], "completed")

    def test_health_fails_while_resident_model_is_unavailable(self):
        process = Mock()
        process.loaded_models.return_value = []
        with patch.object(self.app, "PRELOAD_ENABLED", True), patch.object(self.app, "inference_process", process):
            self.assertEqual(self.app.health().status_code, 503)
            process.loaded_models.return_value = ["large-v3"]
            self.assertEqual(self.app.health()["models_loaded"], ["large-v3"])

    def test_missing_resident_model_rejects_transcription_without_lazy_load(self):
        process = self.app.InferenceProcess()
        with patch.object(self.app, "PRELOAD_ENABLED", True), patch.object(process, "_start_process") as start:
            with self.assertRaises(self.app.TranscriptionUnavailable):
                process.transcribe("next", "/tmp/audio.wav", "large-v3", "", "json")
        start.assert_not_called()
        self.assertTrue(process.run_lock.acquire(blocking=False))
        process.run_lock.release()

    def test_cancel_schedules_resident_model_restoration(self):
        process, tasks = Mock(), Mock()
        process.cancel.return_value = "cancelled"
        with patch.object(self.app, "PRELOAD_ENABLED", True), patch.object(self.app, "inference_process", process):
            response = self.app.cancel(tasks, "active")
        self.assertEqual(response.status_code, 200)
        tasks.add_task.assert_called_once_with(process.preload)

    def test_cancel_unknown_task_does_not_reload_model(self):
        process, tasks = Mock(), Mock()
        process.cancel.return_value = "accepted"
        with patch.object(self.app, "PRELOAD_ENABLED", True), patch.object(self.app, "inference_process", process):
            response = self.app.cancel(tasks, "unknown")
        self.assertEqual(response.status_code, 202)
        tasks.add_task.assert_not_called()

    def test_busy_process_rejects_another_transcription(self):
        process = self.app.InferenceProcess()
        process.run_lock.acquire()
        self.addCleanup(process.run_lock.release)

        with self.assertRaises(self.app.TranscriptionBusy):
            process.transcribe("second", "/tmp/audio.wav", "large-v3", "", "json")

    def test_busy_endpoint_returns_conflict(self):
        self.app.inference_process.run_lock.acquire()
        self.addCleanup(self.app.inference_process.run_lock.release)
        upload = types.SimpleNamespace(filename="audio.wav", file=io.BytesIO(b"audio"))

        with self.assertRaises(self.app.HTTPException) as raised:
            self.app.transcribe(upload, "large-v3", "", "json", "second")

        self.assertEqual(raised.exception.status_code, 409)

    def test_cancel_waits_for_active_task_cleanup(self):
        process = self.app.InferenceProcess()
        task_done = threading.Event()
        fake_process = FakeProcess()
        process.process = fake_process
        process.command_queue = object()
        process.result_queue = object()
        process.active_task_id = "active"
        process.active_task_done = task_done

        result = []
        cancel_thread = threading.Thread(target=lambda: result.append(process.cancel("active")))
        cancel_thread.start()
        self.assertTrue(fake_process.terminated.wait(timeout=1))
        self.assertTrue(cancel_thread.is_alive())

        task_done.set()
        cancel_thread.join(timeout=1)
        self.assertFalse(cancel_thread.is_alive())
        self.assertEqual(result, ["cancelled"])


class FakeProcess:
    def __init__(self):
        self.alive = True
        self.terminated = threading.Event()

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False
        self.terminated.set()

    def join(self, timeout=None):
        return None

    def kill(self):
        self.alive = False


if __name__ == "__main__":
    unittest.main()
