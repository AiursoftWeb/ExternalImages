"""HTTP adapter tests; all scratch files stay beneath the project directory."""
import asyncio
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from fastapi.testclient import TestClient
from PIL import Image

from api import Options, Settings, create_app, offload

ROOT = Path(__file__).resolve().parents[1]


def png(size=(128, 128)):
    stream = io.BytesIO()
    Image.new("RGB", size, "white").save(stream, "PNG")
    return stream.getvalue()


def fake_process(path, outdir, args):
    Image.open(path).save(outdir / (path.stem + ".png"), "PNG")
    result = {"parameters": vars(args), "method": "full_image"}
    return result


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.directory.name)
        self.settings = Settings(self.root / "input", self.root / "output")
        self.client = TestClient(create_app(self.settings))
        self.client.__enter__()
        (self.settings.input_dir / "plot.png").write_bytes(png())
        self.mock = patch("api.process", side_effect=fake_process)
        self.mock.start()

    def tearDown(self):
        self.mock.stop()
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def path_request(self, **options):
        return self.client.post("/process", json={"input_path": "plot.png", **options})

    def assert_clean(self):
        self.assertFalse(list(self.settings.output_dir.glob(".upload-*")))

    def test_path_defaults_download_and_isolation(self):
        self.assertFalse(Options().no_demoire)
        self.assertFalse(Options(no_demoire=False).no_demoire)
        first = self.path_request()
        second = self.path_request()
        self.assertEqual(first.status_code, 200, first.text)
        result = first.json()
        self.assertNotEqual(result["output_path"], second.json()["output_path"])
        self.assertTrue(Path(result["output_path"]).is_file())
        self.assertEqual(result["input_path"], str(self.settings.input_dir / "plot.png"))
        parameters = result["diagnostic"]["parameters"]
        for name, value in Options().model_dump().items():
            self.assertEqual(parameters[name], value)
        image = self.client.get(result["download_url"])
        self.assertEqual(image.headers["content-type"], "image/png")
        self.assertEqual(Image.open(io.BytesIO(image.content)).size, (128, 128))
        self.assertEqual(self.client.get("/results/invalid/image").status_code, 404)
        self.assert_clean()

    def test_options_and_upload_responses(self):
        values = dict(strength=0.7, max_side=320, no_demoire=True, passes=1)
        response = self.path_request(**values)
        self.assertEqual(response.status_code, 200, response.text)
        for key, value in values.items():
            self.assertEqual(response.json()["diagnostic"]["parameters"][key], value)
        for mode in ("json", "image"):
            response = self.client.post("/process/upload", files={"image": ("../../plot.png", png(), "image/png")}, data={"response_type": mode, **{k: str(v).lower() for k, v in values.items()}})
            self.assertEqual(response.status_code, 200, response.text)
            if mode == "image":
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertIn("attachment", response.headers["content-disposition"])
                Image.open(io.BytesIO(response.content)).verify()
            else:
                self.assertEqual(Path(response.json()["output_path"]).name, "image.png")
                self.assertIsNone(response.json()["input_path"])
        self.assert_clean()

    def test_paths_missing_bad_images_and_symlinks(self):
        (self.root / "outside.png").write_bytes(png())
        (self.settings.input_dir / "escape.png").symlink_to(self.root / "outside.png")
        (self.settings.input_dir / "allowed.png").symlink_to(self.settings.input_dir / "plot.png")
        for path in ("../outside.png", str(self.root / "outside.png"), "escape.png", "."):
            self.assertEqual(self.path_request(input_path=path).status_code, 422, path)
        self.assertEqual(self.path_request(input_path="allowed.png").status_code, 200)
        self.assertEqual(self.path_request(input_path="missing.png").status_code, 404)
        (self.settings.input_dir / "bad.jpg").write_bytes(b"not an image")
        self.assertEqual(self.path_request(input_path="bad.jpg").status_code, 422)
        self.assertEqual(self.client.post("/process/upload", files={"image": ("bad.png", b"bad")}).status_code, 422)
        self.assert_clean()

    def test_http_option_boundaries_remain_stricter_than_cli(self):
        for values in ({"strength": 3, "max_side": 4096}, {"strength": 0.001, "max_side": 128}):
            self.assertEqual(self.path_request(**values).status_code, 200, values)
            response = self.client.post("/process/upload", files={"image": ("p.png", png())},
                                        data={key: str(value) for key, value in values.items()})
            self.assertEqual(response.status_code, 200, response.text)
        for values in ({"strength": 30}, {"max_side": 8192}, {"strength": "inf"}):
            self.assertEqual(self.path_request(**values).status_code, 422, values)
            response = self.client.post("/process/upload", files={"image": ("p.png", png())},
                                        data={key: str(value) for key, value in values.items()})
            self.assertEqual(response.status_code, 422, values)

    def test_parameter_rejection(self):
        for values in ({"strength": 0}, {"strength": 3.1}, {"strength": "nan"}, {"max_side": 127}, {"max_side": 4097}, {"passes": 3}, {"passes": 1.5}, {"unknown": 1}, {"gray": False}, {"gray": True}, {"plot_only": False}, {"plot_only": True}):
            self.assertEqual(self.path_request(**values).status_code, 422, values)
            response = self.client.post("/process/upload", files={"image": ("p.png", png())}, data={k: str(v) for k, v in values.items()})
            self.assertEqual(response.status_code, 422, values)
        self.assertEqual(self.client.post("/process/upload", files={"image": ("p.png", png())}, data={"response_type": "bad"}).status_code, 422)
        self.assertEqual(self.client.post("/process/upload", data={"gray": "true"}).status_code, 422)

    def test_limits_and_cleanup(self):
        self.settings.max_pixels = 100
        self.assertEqual(self.path_request().status_code, 413)
        self.settings.max_pixels = 25_000_000
        self.settings.max_file_bytes = 10
        self.assertEqual(self.path_request().status_code, 413)
        self.assertEqual(self.client.post("/process/upload", files={"image": ("p.png", png())}).status_code, 413)
        self.assertEqual(self.client.post("/process/upload", content=b"a" * 70000, headers={"Content-Type": "multipart/form-data; boundary=x"}).status_code, 413)
        self.assert_clean()
        self.assertEqual(list(self.settings.output_dir.iterdir()), [])

    def test_failure_recovery_and_download_containment(self):
        def fail(path, outdir, args):
            (outdir / "partial.png").write_bytes(b"bad")
            raise RuntimeError("secret details")
        with patch("api.process", side_effect=fail):
            response = self.path_request()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret", response.text)
        self.assertEqual(list(self.settings.output_dir.iterdir()), [])
        result = self.path_request().json()
        image = Path(result["output_path"])
        image.unlink()
        image.symlink_to(self.settings.input_dir / "plot.png")
        self.assertEqual(self.client.get(result["download_url"]).status_code, 404)
        self.assert_clean()

    def test_busy_and_health_remain_responsive(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return fake_process(*args)
        responses = []
        with patch("api.process", side_effect=slow):
            thread = threading.Thread(target=lambda: responses.append(self.path_request()))
            thread.start()
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.path_request().status_code, 503)
                self.assertEqual(self.client.post("/process/upload", files={"image": ("p.png", png())}).status_code, 503)
                self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(self.path_request().status_code, 200)

    def test_uuid_collision_does_not_delete_existing_result(self):
        from uuid import UUID
        result = self.path_request().json()
        output = Path(result["output_path"])
        original = output.read_bytes()
        with patch("api.uuid4", return_value=UUID(result["request_id"])):
            self.assertEqual(self.path_request().status_code, 500)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(self.client.get(result["download_url"]).status_code, 200)
        self.assert_clean()

    def test_results_survive_new_app_instance(self):
        result = self.path_request().json()
        with TestClient(create_app(self.settings)) as restarted:
            response = restarted.get(result["download_url"])
            self.assertEqual(response.status_code, 200)
            Image.open(io.BytesIO(response.content)).verify()

    def test_configurable_two_slots(self):
        settings = Settings(self.settings.input_dir, self.settings.output_dir, processing_slots=2)
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        lock = threading.Lock()
        calls = []
        def slow(*args):
            with lock:
                index = len(calls)
                calls.append(index)
            entered[index].set()
            release.wait(5)
            return fake_process(*args)
        responses = []
        with TestClient(create_app(settings)) as client, patch("api.process", side_effect=slow):
            threads = [threading.Thread(target=lambda: responses.append(client.post("/process", json={"input_path": "plot.png"}))) for _ in range(2)]
            for thread in threads:
                thread.start()
            try:
                self.assertTrue(all(event.wait(5) for event in entered))
                self.assertEqual(client.post("/process", json={"input_path": "plot.png"}).status_code, 503)
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)
        self.assertEqual([response.status_code for response in responses], [200, 200])



class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_held_until_cancelled_work_finishes(self):
        directory = tempfile.TemporaryDirectory(dir=ROOT)
        settings = Settings(Path(directory.name) / "input", Path(directory.name) / "output")
        app = create_app(settings)
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            release.wait(5)
            return fake_process(*args)
        try:
            async with app.router.lifespan_context(app):
                (settings.input_dir / "plot.png").write_bytes(png())
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    with patch("api.process", side_effect=slow):
                        task = asyncio.create_task(client.post("/process", json={"input_path": "plot.png"}))
                        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                        task.cancel()
                        await asyncio.sleep(0.05)
                        self.assertFalse(task.done())
                        self.assertEqual((await client.post("/process", json={"input_path": "plot.png"})).status_code, 503)
                        release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    self.assertEqual(list(settings.output_dir.iterdir()), [])
                    with patch("api.process", side_effect=fake_process):
                        self.assertEqual((await client.post("/process", json={"input_path": "plot.png"})).status_code, 200)
        finally:
            release.set()
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
