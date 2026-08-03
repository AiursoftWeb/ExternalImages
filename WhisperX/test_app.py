import importlib
import io
import os
import sys
import threading
import types
import unittest


class FakeFastAPI:
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


def load_app_module():
    module_names = ["whisperx", "fastapi", "fastapi.responses", "models_config"]
    original_modules = {name: sys.modules.get(name) for name in module_names}
    original_token = os.environ.get("ASR_WHISPERX_TOKEN")
    whisperx = types.ModuleType("whisperx")
    fastapi = types.ModuleType("fastapi")
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
        sys.modules["whisperx"] = whisperx
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
