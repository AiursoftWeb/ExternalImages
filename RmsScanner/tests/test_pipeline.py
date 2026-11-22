"""Protocol and compatibility tests with generated images, no stored fixtures."""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

import pipeline
import scan

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def work():
    with tempfile.TemporaryDirectory(dir=ROOT) as directory:
        yield Path(directory)


def test_three_stage_protocol_offline(work):
    source = work / "input.png"
    Image.new("RGB", (128, 128), "white").save(source)
    calls = []

    def runner(command, cwd, out, err):
        calls.append(command)
        assert all(flag in command for flag in ("--offline", "--no-sync", "--frozen"))
        destination = Path(command[command.index("--output") + 1])
        stage = pipeline.STAGES[len(calls) - 1][0] if len(calls) < 3 else "chart-enhance"
        if stage == "preprocess":
            current = Path(command[command.index("image-preprocess") + 1])
            assert command[command.index("--max-side") + 1] == "2048"
        elif stage == "demoire":
            current = Path(command[command.index("infer") + 1])
            assert command[command.index("--device") + 1] == "cpu"
            assert command[command.index("--max-side") + 1] == "0"
        else:
            current = Path(command[command.index("process") + 1])
        output = destination / "output.png"
        Image.open(current).save(output)
        out.write_text(json.dumps(dict(protocol_version=1, stage=stage, status="ok", input=str(current), output=str(output))))
        err.write_text("")
        return 0

    code, manifest = pipeline.execute(source, work / "output", runner=runner)
    assert code == 0
    report = json.loads(manifest.read_text())
    assert [s["stage"] for s in report["images"][0]["stages"]] == ["preprocess", "demoire", "chart-enhance"]


@pytest.mark.parametrize("mutation", ["version", "input", "escape", "status", "extra", "code"])
def test_reject_bad_stage_protocol(work, mutation):
    source, output = work / "input.png", work / "output.png"
    source.touch(); output.touch()
    destination = work / "destination"
    destination.mkdir()
    good = destination / "good.png"
    good.touch()
    record = dict(protocol_version=1, stage="preprocess", status="ok", input=str(source), output=str(good))
    if mutation == "version": record["protocol_version"] = True
    if mutation == "input": record["input"] = str(output)
    if mutation == "escape": record["output"] = str(output)
    if mutation == "status": record["status"] = "error"
    text = json.dumps(record)
    if mutation == "extra": text += "\n{}"
    with pytest.raises(ValueError):
        pipeline.validate_record(text, "preprocess", source, destination, 1 if mutation == "code" else 0)


def test_legacy_options_ignored_and_intermediates_cleaned(work):
    source = work / "upload.image"
    Image.new("RGB", (128, 128)).save(source, "PNG")
    calls = []

    def execute(path, output_root, **kwargs):
        calls.append(kwargs)
        output = output_root / "final.png"
        Image.open(path).save(output)
        manifest = output_root / "manifest.json"
        manifest.write_text(json.dumps({"status": "ok", "images": [{"status": "ok", "output": str(output), "stages": [
            {"stage": stage, "status": "ok", "result": {"input": str(path), "output": str(output), "metadata": "ephemeral"}}
            for stage in ("preprocess", "demoire", "chart-enhance")]}]}))
        return 0, manifest

    with patch("scan.pipeline.execute", side_effect=execute):
        for args in (SimpleNamespace(strength=0.5, max_side=128, no_demoire=True, passes=1),
                     SimpleNamespace(strength=30, max_side=8192, no_demoire=False, passes=2)):
            diagnostic = scan.process(source, work / "result", args)
            assert diagnostic["parameters"]["max_side"] == 2048
            assert len(diagnostic["stages"]) == 3
    assert calls[0] == calls[1]
    assert not list((work / "result").glob(".pipeline-*"))
    assert (work / "result/upload.png").is_file()
    with pytest.raises(ValueError):
        scan.process(source, work / "invalid", SimpleNamespace(passes=3))


def test_failure_never_falls_back_and_cleans(work):
    source = work / "input.png"
    Image.new("RGB", (128, 128)).save(source)
    def fail(path, output_root, **kwargs):
        manifest = output_root / "manifest.json"
        manifest.write_text(json.dumps({"status": "partial_failure", "images": [{"status": "error", "error": "Chart not found"}]}))
        return 1, manifest
    with patch("scan.pipeline.execute", side_effect=fail), pytest.raises(RuntimeError, match="Chart not found"):
        scan.process(source, work / "result", SimpleNamespace())
    assert list((work / "result").iterdir()) == []


def test_subprocess_failure_is_waited_and_logs_persist(work):
    code = pipeline.run_process([os.sys.executable, "-c", "import sys; print('failure', file=sys.stderr); sys.exit(7)"], work, work / "out", work / "err")
    assert code == 7
    assert "failure" in (work / "err").read_text()
