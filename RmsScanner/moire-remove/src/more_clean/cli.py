"""Local batch inference and Web entry points."""

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from .image_io import SUPPORTED_SUFFIXES, format_file_size, save_png_exclusive
from .service import DemoireService

app = typer.Typer(no_args_is_help=True, help="本地 MBCNN 去摩尔纹工具")


@app.callback()
def main():
    """CPU inference commands."""


@app.command()
def infer(
    image_or_dir: Annotated[Path, typer.Argument(help="图片或目录（不递归）")],
    output: Annotated[Path, typer.Option(help="PNG 输出目录，不覆盖已有文件")] = Path(
        "outputs"
    ),
    weights: Annotated[Path | None, typer.Option(help="本地 HDF5 权重路径")] = None,
    device: Annotated[str, typer.Option(help="auto、cuda 或 cpu")] = "auto",
    max_side: Annotated[
        int, typer.Option(min=0, help="可选缩图最长边；默认 0，直接使用输入尺寸")
    ] = 0,
    json_output: Annotated[
        bool, typer.Option("--json", help="输出版本化 JSONL，日志写 stderr")
    ] = False,
    auto_resize: Annotated[
        bool, typer.Option(help="显存不足时允许最多三次缩小重试")
    ] = False,
):
    """处理单张图片或目录中的 PNG/JPEG/静态 WebP。"""
    try:
        if image_or_dir.is_dir():
            paths = sorted(
                (
                    p
                    for p in image_or_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
                ),
                key=lambda p: (p.name.casefold(), p.name),
            )
        elif image_or_dir.is_file():
            paths = [image_or_dir]
        else:
            raise ValueError(f"输入路径不存在: {image_or_dir}")
        if not paths:
            raise ValueError("目录中没有支持的图片（不递归扫描）")
        service = DemoireService(weights=weights, device=device, cpu_threads=int(os.getenv("OPENCV_THREADS", "2")))
    except Exception as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "protocol_version": 1,
                        "stage": "demoire",
                        "status": "error",
                        "input": str(image_or_dir.resolve()),
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
            )
        typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(1) from exc
    failures = 0
    for path in paths:
        try:
            result = service.process(path, max_side=max_side, auto_resize=auto_resize)
            saved = save_png_exclusive(result.output, output, path.stem)
            if json_output:
                typer.echo(
                    json.dumps(
                        {
                            "protocol_version": 1,
                            "stage": "demoire",
                            "status": "ok",
                            "input": str(path.resolve()),
                            "output": str(saved.resolve()),
                            "original_size": list(result.original_size),
                            "processed_size": list(result.processed_size),
                            "output_size": list(result.output.size),
                            "input_bytes": path.stat().st_size,
                            "output_bytes": saved.stat().st_size,
                            "device": result.device,
                            "elapsed_seconds": result.elapsed_seconds,
                        },
                        ensure_ascii=False,
                    )
                )
            memory = (
                f"{result.peak_memory_bytes / 1024**2:.1f} MiB"
                if result.peak_memory_bytes is not None
                else "N/A (CPU)"
            )
            typer.echo(
                f"{path.name} -> {saved} | 原图 {result.original_size} → "
                f"处理 {result.processed_size} → 输出 {result.output.size} | "
                f"请求设备 {device}；实际设备 {result.device} | "
                f"{result.elapsed_seconds:.2f}s | 峰值显存 {memory} | "
                f"文件大小 {format_file_size(path.stat().st_size)} → "
                f"{format_file_size(saved.stat().st_size)}（PNG）",
                err=json_output,
            )
            for notice in result.notices:
                typer.echo(f"提示: {notice}", err=True)
        except Exception as exc:  # noqa: BLE001 - each failed file must leave the batch running
            failures += 1
            if json_output:
                typer.echo(
                    json.dumps(
                        {
                            "protocol_version": 1,
                            "stage": "demoire",
                            "status": "error",
                            "input": str(path.resolve()),
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    )
                )
            target = "原尺寸" if max_side == 0 else f"最长边 {max_side}"
            typer.echo(
                f"失败 {path.name}（请求设备 {device}；目标 {target}；"
                f"自动缩小 {'开启' if auto_resize else '关闭'}；本次未生成输出）: {exc}",
                err=True,
            )
    typer.echo(f"完成: {len(paths) - failures} 成功，{failures} 失败", err=json_output)
    if failures:
        raise typer.Exit(1)
