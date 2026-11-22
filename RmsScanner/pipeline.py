"""File/JSON CLI orchestration. No imports from independent stage projects."""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
import time
import job_logging as events
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
CHART_STAGE = ("chart-enhance", "chart-enhance", "chart-enhance")
STAGES = (
    ("preprocess", "image-preprocess", "image-preprocess"),
    ("demoire", "moire-remove", "more-clean"),
)


def write_manifest(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def run_process(command, cwd, stdout_path, stderr_path):
    with (
        stdout_path.open("x", encoding="utf-8") as out,
        stderr_path.open("x", encoding="utf-8") as err,
    ):
        child = subprocess.Popen(
            command, cwd=cwd, stdout=out, stderr=err, start_new_session=True
        )
        try:
            return child.wait()
        except BaseException:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
            raise


def validate_record(text, stage, source, destination, code):
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("阶段必须为单图返回且仅返回一条 JSON 记录")
    record = json.loads(lines[0])
    if (
        not isinstance(record, dict)
        or type(record.get("protocol_version")) is not int
        or record["protocol_version"] != 1
    ):
        raise ValueError("不支持的阶段协议版本")
    if record.get("stage") != stage or record.get("input") != str(source.resolve()):
        raise ValueError("阶段或输入路径与当前任务不匹配")
    if code != 0 or record.get("status") != "ok":
        raise ValueError(record.get("error", f"阶段失败，退出码 {code}"))
    output = record.get("output")
    if not isinstance(output, str) or not Path(output).is_absolute():
        raise ValueError("阶段输出必须为绝对文件路径")
    path = Path(output).resolve(strict=True)
    if (
        not path.is_relative_to(destination.resolve())
        or not path.is_file()
        or path.suffix.lower() != ".png"
    ):
        raise ValueError("阶段输出不在本任务目录中或不是 PNG 文件")
    if path == source.resolve():
        raise ValueError("阶段不得覆盖输入")
    return record, path


def preflight(weights, chart_enhance):
    started = time.monotonic()
    events.emit("preflight", "begin", stage="preflight", started=started)
    try:
        uv = shutil.which("uv")
        if uv is None:
            raise ValueError("未找到 uv；请安装并分别运行两个项目的 uv sync --locked")
        stages = STAGES + (CHART_STAGE,) if chart_enhance else STAGES
        for _, project, _ in stages:
            for filename in ("pyproject.toml", "uv.lock"):
                if not (ROOT / project / filename).is_file():
                    raise ValueError(f"缺少独立项目文件：{project}/{filename}")
        if weights is not None and not weights.is_file():
            raise ValueError(f"权重不存在：{weights}")
        if weights is None:
            weights = Path(os.environ.get("MODEL_WEIGHTS", str(ROOT / "models/default.h5")))
            if not weights.is_file():
                raise ValueError(f"权重不存在：{weights}")
        events.emit("preflight", "ok", stage="preflight", started=started)
        return uv, stages, weights
    except Exception as exc:
        events.emit("preflight", "fail", stage="preflight", started=started, reason=exc)
        raise


def execute(*args, **kwargs):
    started = time.monotonic()
    try:
        return _execute(*args, **kwargs)
    except BaseException as exc:
        events.emit("pipeline", "cancelled" if isinstance(exc, (KeyboardInterrupt, events.JobCancelled)) else "fail",
                    started=started, reason=exc)
        raise


def _execute(
    input_path,
    output_root,
    max_side=2048,
    device="cpu",
    weights=None,
    runner=run_process,
    chart_enhance=True,
):
    uv, stages, weights = preflight(weights, chart_enhance)
    source = input_path.resolve()
    if source.is_dir():
        files = sorted(
            (
                p
                for p in source.iterdir()
                if p.is_file() and p.suffix.lower() in SUFFIXES
            ),
            key=lambda p: (p.name.casefold(), p.name),
        )
    elif source.is_file():
        files = [source]
    else:
        raise ValueError(f"输入不存在：{source}")
    if not files:
        raise ValueError("没有可处理的图片（目录不递归）")
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    batch = output_root / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    batch.mkdir(exist_ok=False)
    report = {
        "protocol_version": 1,
        "status": "running",
        "batch": str(batch),
        "parameters": {
            "max_side": max_side,
            "device": device,
            "weights": str(weights) if weights else None,
            "chart_enhance": chart_enhance,
        },
        "images": [],
    }
    manifest = batch / "manifest.json"
    write_manifest(manifest, report)
    try:
        for index, image in enumerate(files, 1):
            job = batch / f"image-{index:05d}"
            job.mkdir()
            item = {"input": str(image), "status": "running", "stages": []}
            report["images"].append(item)
            current = image
            for number, (stage, project, entry) in enumerate(stages, 1):
                destination = job / f"{number:02d}-{stage}"
                destination.mkdir()
                command = [
                    uv,
                    "run",
                    "--frozen",
                    "--no-sync",
                    "--offline",
                    "--project",
                    str(ROOT / project),
                    entry,
                ]
                if stage == "demoire":
                    command += [
                        "infer",
                        str(current),
                        "--output",
                        str(destination),
                        "--max-side",
                        "0",
                        "--device",
                        device,
                        "--json",
                    ]
                    if weights:
                        command += ["--weights", str(weights.resolve())]
                elif stage == "chart-enhance":
                    command += [
                        "process",
                        str(current),
                        "--output",
                        str(destination),
                        "--json",
                    ]
                else:
                    command += [
                        str(current),
                        "--output",
                        str(destination),
                        "--max-side",
                        str(max_side),
                    ]
                out, err = job / f"{stage}.stdout.jsonl", job / f"{stage}.stderr.log"
                result = {
                    "stage": stage,
                    "status": "running",
                    "command": command,
                    "stdout": str(out),
                    "stderr": str(err),
                }
                item["stages"].append(result)
                write_manifest(manifest, report)
                started = time.monotonic()
                events.check_cancelled()
                events.emit("stage", "begin", stage=stage, started=started)
                try:
                    code = runner(command, ROOT / project, out, err)
                    record, current = validate_record(
                        out.read_text(encoding="utf-8"),
                        stage,
                        current,
                        destination,
                        code,
                    )
                    result.update(status="ok", exit_code=code, result=record)
                    events.emit("stage", "ok", stage=stage, output=current, started=started)
                    events.check_cancelled()
                except (KeyboardInterrupt, events.JobCancelled) as exc:
                    events.emit("stage", "cancelled", stage=stage, started=started, reason=exc)
                    raise
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    events.emit("stage", "fail", stage=stage, started=started, reason=exc)
                    detail = str(exc)
                    if err.is_file():
                        detail += "\n" + err.read_text(encoding="utf-8", errors="replace")[-8000:]
                    result.update(status="error", error=detail)
                    item.update(status="error", error=detail)

                    break
            else:
                item.update(status="ok", output=str(current))

            write_manifest(manifest, report)
        report["succeeded"] = sum(row["status"] == "ok" for row in report["images"])
        report["failed"] = len(files) - report["succeeded"]
        report["status"] = "ok" if not report["failed"] else "partial_failure"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        for item in report["images"]:
            if item["status"] == "running":
                item["status"] = "interrupted"
                for stage in item["stages"]:
                    if stage["status"] == "running":
                        stage["status"] = "interrupted"
        write_manifest(manifest, report)
        events.emit("pipeline", "cancelled", reason="KeyboardInterrupt")
        return 130, manifest
    write_manifest(manifest, report)

    return (0 if report["status"] == "ok" else 1), manifest


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fixed CPU pipeline: preprocess → MBCNN demoire → chart-enhance"
    )
    parser.add_argument("input", type=Path, help="图片或目录（不递归）")
    parser.add_argument("--output", type=Path, default=Path("runs"))
    parser.add_argument("--max-side", type=int, default=2048)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="cpu")
    parser.add_argument("--weights", type=Path)
    parser.add_argument(
        "--chart-enhance",
        action="store_true",
        default=True,
        help="Compatibility flag; chart enhancement is always enabled",
    )
    args = parser.parse_args(argv)
    if args.max_side < 1:
        parser.error("--max-side 必须为正整数")
    try:
        code, _ = execute(
            args.input,
            args.output,
            2048,
            "cpu",
            args.weights.resolve() if args.weights else None,
            chart_enhance=True,
        )
        return code
    except (OSError, ValueError) as exc:
        print(f"管线错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
