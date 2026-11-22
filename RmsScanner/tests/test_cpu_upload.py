"""Exercise the same multipart route and offline subprocesses used by RMS."""
import io

from fastapi.testclient import TestClient
from PIL import Image

from api import Settings, create_app
from test_chart_core import chart


def test_cpu_upload(tmp_path):
    photo = io.BytesIO()
    Image.fromarray(chart()).save(photo, "JPEG")
    settings = Settings(tmp_path / "input", tmp_path / "output")
    with TestClient(create_app(settings)) as client:
        result = client.post(
            "/process/upload",
            files={"image": ("chart.jpg", photo.getvalue(), "image/jpeg")},
            data={"response_type": "image"},
        )
        assert result.status_code == 200, result.text[:300]
        assert result.headers["content-type"] == "image/png"
        with Image.open(io.BytesIO(result.content)) as image:
            assert image.format == "PNG"
            image.verify()
        persisted = client.get(f"/results/{result.headers['x-request-id']}/image")
        assert persisted.content == result.content
