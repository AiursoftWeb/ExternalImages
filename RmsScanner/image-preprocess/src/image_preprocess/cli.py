"""Emit one JSON object per file to stdout for downstream pipeline stages."""

import argparse
import json
import sys
from pathlib import Path

from .core import SUPPORTED, preprocess


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="独立图片预处理：等比例缩至长边最多 2048，另存 RGB PNG"
    )
    parser.add_argument("input", type=Path, help="单张图片或目录（不递归）")
    parser.add_argument("--output", type=Path, required=True, help="PNG 输出目录")
    parser.add_argument(
        "--max-side", type=int, default=2048, help="最长边上限，默认 2048，不放大小图"
    )
    args = parser.parse_args(argv)
    if args.max_side < 1:
        parser.error("--max-side 必须大于 0")
    if args.input.is_dir():
        if args.input.resolve() == args.output.resolve():
            parser.error("目录批处理的输出目录不能与输入目录相同")
        files = sorted(
            (p for p in args.input.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED),
            key=lambda p: (p.name.casefold(), p.name),
        )
    elif args.input.is_file():
        files = [args.input]
    else:
        parser.error("输入路径不存在")
    if not files:
        parser.error("未找到支持的图片")
    failed = 0
    for source in files:
        try:
            result = preprocess(source, args.output, args.max_side)
            print(
                json.dumps(
                    {"protocol_version": 1, "stage": "preprocess", "status": "ok", **result},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - continue independent batch files at CLI boundary
            failed += 1
            print(
                json.dumps(
                    {
                        "protocol_version": 1,
                        "stage": "preprocess",
                        "status": "error",
                        "input": str(source.resolve()),
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            print(f"预处理失败 {source.name}: {exc}", file=sys.stderr)
    print(f"完成：{len(files) - failed} 成功，{failed} 失败", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
