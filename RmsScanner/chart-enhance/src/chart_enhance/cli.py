import argparse
import json
import os

import cv2
import sys
from pathlib import Path

from .core import process


def parse_corners(value):
    try:
        path = Path(value)
        if len(value) < 240 and path.is_file():
            value = path.read_text()
        if value.lstrip().startswith("["):
            points = json.loads(value)
        else:
            nums = [float(x.strip()) for x in value.split(",")]
            points = [nums[i : i + 2] for i in range(0, len(nums), 2)]
        if len(points) != 4 or any(len(p) != 2 for p in points):
            raise ValueError("expected four coordinate pairs")
        return points
    except (ValueError, OSError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"Invalid corners: {exc}") from exc


def main():
    parser = argparse.ArgumentParser(description="Conservative chart rectification")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("process")
    p.add_argument("input", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--corners",
        type=parse_corners,
        help="JSON file, nested JSON list or x1,y1,... in TL TR BR BL order",
    )
    p.add_argument("--no-whiten", action="store_true")
    p.add_argument("--sharpen-strength", type=float, default=0.3)
    args = parser.parse_args()
    cv2.setNumThreads(int(os.getenv("OPENCV_THREADS", "2")))
    paths = (
        sorted(
            p
            for p in args.input.iterdir()
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
        )
        if args.input.is_dir()
        else [args.input]
    )
    if not paths:
        print("No supported images", file=sys.stderr)
        raise SystemExit(2)
    failed = False
    for path in paths:
        try:
            record = process(
                path,
                args.output,
                args.corners,
                not args.no_whiten,
                args.sharpen_strength,
            )
        except (ValueError, OSError, RuntimeError) as exc:
            failed = True
            record = {
                "protocol_version": 1,
                "stage": "chart-enhance",
                "status": "error",
                "input": str(path.resolve()),
                "error": str(exc),
            }
            print(f"{path.name}: {exc}", file=sys.stderr)
        print(
            json.dumps(record)
            if args.json
            else f"{record['status']}: {record.get('output', record.get('error'))}"
        )
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
