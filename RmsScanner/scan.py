"""Compatibility entry point for the fixed CPU curve-chart-beauty pipeline."""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

import pipeline
import time
from PIL import Image
import job_logging as events


def validate_cli_options(args):
    """Keep legacy CLI ranges separate from the stricter HTTP Options model."""
    strength = getattr(args, "strength", 1.0)
    if (getattr(args, "max_side", 2400) < 128 or not math.isfinite(strength)
            or strength <= 0 or getattr(args, "threads", 4) < 1):
        raise ValueError("max-side >= 128, finite strength > 0, threads >= 1 required")
    if getattr(args, "passes", 2) not in (1, 2):
        raise ValueError("passes must be 1 or 2")
    if getattr(args, "corners", None) and getattr(args, "no_crop", False):
        raise ValueError("--corners and --no-crop cannot be combined")


def process(path, outdir, args):
    """Return diagnostics in memory; publish only a completely generated PNG."""
    started = time.monotonic()
    standalone = not events.has_context()
    metadata = {"inputName": Path(path).name} if standalone else {}
    with events.context(**metadata):
        try:
            if standalone:
                events.emit("receipt", "ok", started=started)
                events.emit("validation", "begin", started=started)
            with Image.open(path) as image:
                dimensions = {"width": image.width, "height": image.height}
            events.enrich(sizeBytes=Path(path).stat().st_size, **dimensions)
            if standalone:
                events.emit("validation", "ok", started=started)
            events.emit("processing", "begin", started=started)
            result = _process(path, outdir, args, started)
            events.emit("processing", "ok", started=started, output=result["output"])
            if standalone:
                events.emit("job", "ok", started=started, output=result["output"])
            return result
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, (KeyboardInterrupt, events.JobCancelled)) else "fail"
            events.emit("processing", status, started=started, reason=exc)
            if standalone:
                events.emit("job", status, started=started, reason=exc)
            raise
        finally:
            events.emit("cleanup", "ok", started=started)


def _process(path, outdir, args, started):
    validate_cli_options(args)
    path, outdir = Path(path).resolve(), Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    weights = Path(os.environ.get("MODEL_WEIGHTS", str(pipeline.ROOT / "models/default.h5"))).resolve()
    with tempfile.TemporaryDirectory(prefix=".pipeline-", dir=outdir) as scratch:
        stage_input = Path(scratch) / "source.png"
        shutil.copyfile(path, stage_input)
        code, manifest = pipeline.execute(stage_input, Path(scratch), max_side=2048, device="cpu",
                                          weights=weights, chart_enhance=True)
        report = json.loads(manifest.read_text())
        if code == 130:
            raise events.JobCancelled("Pipeline interrupted")
        if code or report["status"] != "ok":
            errors = [item.get("error", "Pipeline interrupted") for item in report["images"] if item["status"] != "ok"]
            raise RuntimeError("Fixed pipeline failed: " + "; ".join(errors))
        item = report["images"][0]
        destination = outdir / (path.stem + ".png")
        # Do not expose paths to ephemeral intermediate images as persistent artifacts.
        stages = []
        for stage in item["stages"]:
            record = stage["result"]
            stages.append({"stage": stage["stage"], "status": stage["status"],
                           "result": {k: v for k, v in record.items() if k not in {"input", "output", "artifacts", "metadata"}}})
        diagnostic = {"method": "curve-chart-beauty", "input": str(path), "output": str(destination),
                      "parameters": {"max_side": 2048, "device": "cpu", "chart_enhance": True,
                                     "weights": str(weights)}, "stages": stages,
                      "legacy_options": "Accepted and validated, ignored for processing"}
        events.check_cancelled()
        pending = Path(scratch) / "committed.png"
        shutil.copyfile(item["output"], pending)
        pending.replace(destination)
        events.emit("final_output", "ok", output=destination, started=started)
        return diagnostic


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", "--outdir", type=Path, default=Path("output"))
    parser.add_argument("--strength", type=float, default=1.0, help="Compatibility only; ignored")
    parser.add_argument("--max-side", type=int, default=2400, help="Compatibility only; ignored")
    parser.add_argument("--no-demoire", action="store_true", help="Compatibility only; ignored")
    parser.add_argument("--demoire", dest="no_demoire", action="store_false", help="Compatibility only; ignored")
    parser.add_argument("--passes", type=int, default=2, help="Compatibility only; ignored")
    parser.add_argument("--no-crop", action="store_true", help="Compatibility only; ignored")
    parser.add_argument("--corners", help="Compatibility only; ignored")
    parser.add_argument("--debug", action="store_true", help="Compatibility only; ignored")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        validate_cli_options(args)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.input.exists():
        parser.error(f"Input does not exist: {args.input}")
    files = sorted(p for p in args.input.iterdir() if p.suffix.lower() in pipeline.SUFFIXES) if args.input.is_dir() else [args.input]
    if not files:
        parser.error("No input images")
    if args.corners and len(files) > 1:
        parser.error("--corners requires a single input image")
    if len({path.stem for path in files}) != len(files):
        parser.error("Input basenames collide; process the images separately into different output directories")
    destinations = {
        (args.output / (path.stem + suffix)).resolve()
        for path in files for suffix in (".png", ".json")
    }
    if destinations.intersection(path.resolve() for path in files):
        parser.error("Output would overwrite an input image; choose another output directory")
    os.environ["OPENCV_THREADS"] = str(args.threads)
    try:
        for path in files:
            process(path, args.output, args)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Processing failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
