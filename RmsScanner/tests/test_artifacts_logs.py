"""Final-artifact lifecycle and live parent-process event contract."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import api
import job_logging as events
import pipeline
import scan


def records(capsys):
    return [json.loads(line) for line in capsys.readouterr().err.splitlines()]


def fake_runner(command, cwd, out, err):
    stage = {"image-preprocess": "preprocess", "moire-remove": "demoire", "chart-enhance": "chart-enhance"}[cwd.name]
    offset = 1 if stage == "preprocess" else 2
    source = Path(command[command.index(str(cwd)) + 1 + offset])
    destination = Path(command[command.index("--output") + 1]) / "final.png"
    Image.open(source).save(destination)
    out.write_text(json.dumps(dict(protocol_version=1, stage=stage, status="ok", input=str(source), output=str(destination))))
    err.write_text("")
    return 0


def test_live_stages_artifacts_and_original_name(tmp_path, capsys):
    settings = api.Settings(tmp_path / "input", tmp_path / "output")
    original_execute = pipeline.execute
    seen = []
    def runner(*args):
        # The begin event must already be emitted before actual stage execution.
        current = records(capsys)
        seen.extend(current)
        assert current[-1]["event"] == "stage" and current[-1]["status"] == "begin"
        return fake_runner(*args)
    def execute(*args, **kwargs):
        return original_execute(*args, runner=runner, **kwargs)
    with TestClient(api.create_app(settings)) as client, patch("scan.pipeline.execute", side_effect=execute):
        source = tmp_path / "source.png"
        Image.new("RGB", (128, 96)).save(source)
        name = 'untrusted"\nname.png'
        result = client.post("/process/upload", files={"image": (name, source.read_bytes())}).json()
        output = Path(result["output_path"])
        assert sorted(p.name for p in output.parent.iterdir()) == [".result.json", "image.png"]
        assert json.loads((output.parent / ".result.json").read_text()) == {"output_path": str(output)}
        assert len(result["diagnostic"]["stages"]) == 3
        assert client.get(result["download_url"]).status_code == 200
        # Legacy full response indexes are still readable.
        (output.parent / ".result.json").write_text(json.dumps(result))
        assert client.get(result["download_url"]).status_code == 200
    with TestClient(api.create_app(settings)) as restarted:
        assert restarted.get(result["download_url"]).status_code == 200
    seen.extend(records(capsys))
    assert all(row["requestId"] == result["request_id"] for row in seen)
    assert all(row.get("inputName") != "image.image" for row in seen)
    assert [(r["stage"], r["status"]) for r in seen if r["event"] == "stage"] == [(s, status) for s in ("preprocess", "demoire", "chart-enhance") for status in ("begin", "ok")]
    assert not any(r["event"] in {"receipt", "validation", "processing", "final_output"} for r in seen)
    assert any(r["event"] == "job" and r.get("inputName") for r in seen)
    assert all(r.get("elapsedMilliseconds", 0) >= 0 for r in seen)
    assert all(not ({"sizeBytes", "width", "height", "output"} & r.keys()) for r in seen)
    assert not list(settings.output_dir.glob(".upload-*"))


@pytest.mark.parametrize("failure", ["stage", "preflight", "cancel"])
def test_failure_cleanup_preserves_success(tmp_path, capsys, failure):
    source = tmp_path / "source.png"
    Image.new("RGB", (128, 128)).save(source)
    output = tmp_path / "output"
    output.mkdir()
    existing = output / "source.png"
    existing.write_bytes(b"previous success")
    original_execute = pipeline.execute
    def runner(command, cwd, out, err):
        if cwd.name == "moire-remove":
            if failure == "cancel":
                raise KeyboardInterrupt
            raise ValueError("demoire failure")
        return fake_runner(command, cwd, out, err)
    def execute(*args, **kwargs):
        return original_execute(*args, runner=runner, **kwargs)
    context = patch("pipeline.shutil.which", return_value=None) if failure == "preflight" else patch("scan.pipeline.execute", side_effect=execute)
    with context, pytest.raises((RuntimeError, ValueError)):
        scan.process(source, output, SimpleNamespace())
    assert list(output.iterdir()) == [existing]
    assert existing.read_bytes() == b"previous success"
    logs = records(capsys)
    if failure == "stage":
        assert any(r.get("stage") == "demoire" and r["status"] == "fail" for r in logs)
        assert not any(r.get("stage") == "chart-enhance" for r in logs)
    elif failure == "preflight":
        assert any(r.get("stage") == "preflight" and r["status"] == "fail" for r in logs)
    else:
        assert any(r["status"] == "cancelled" for r in logs)


def test_cli_only_png(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (128, 128)).save(source)
    original_execute = pipeline.execute
    def execute(*args, **kwargs):
        return original_execute(*args, runner=fake_runner, **kwargs)
    output = tmp_path / "output"
    with patch("scan.pipeline.execute", side_effect=execute):
        assert scan.main([str(source), "-o", str(output)]) == 0
    assert [p.name for p in output.iterdir()] == ["source.png"]


def test_packaging_includes_event_module():
    root = Path(api.__file__).parent
    assert "COPY scan.py pipeline.py api.py job_logging.py ./" in (root / "Dockerfile").read_text()
    assert not any(line in {"*.py", "job_logging.py"} for line in (root / ".dockerignore").read_text().splitlines())
