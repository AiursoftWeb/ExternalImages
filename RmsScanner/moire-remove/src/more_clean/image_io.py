"""Bounded image decoding and lossless, non-overwriting output helpers."""

from __future__ import annotations

import struct
import warnings
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

SUPPORTED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MAX_IMAGE_PIXELS = 24_000_000
MAX_UPLOAD_BYTES = 40 * 1024 * 1024


class ImageInputError(ValueError):
    """An image cannot be decoded safely as a supported 8-bit still image."""


def load_image(path: str | Path) -> Image.Image:
    """Decode, orient and convert a supported still image to RGB on white."""
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ImageInputError("只支持 PNG、JPEG 和静态 WebP 图片")
    try:
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            raise ImageInputError("图片文件超过 40 MiB 限制")
        with path.open("rb") as stream:
            header = stream.read(29)
        if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 25:
            if header[24] > 8:
                raise ImageInputError("不支持超过 8 位的图片；请先转换为 8 位 PNG/JPEG")
            width, height = struct.unpack(">II", header[16:24])
            if width * height > MAX_IMAGE_PIXELS:
                raise ImageInputError("图片解码像素超过 2400 万限制")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ImageInputError("图片内容不是 PNG、JPEG 或 WebP")
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ImageInputError("图片解码像素超过 2400 万限制")
                if getattr(source, "n_frames", 1) != 1:
                    raise ImageInputError("不支持动画图片")
                if source.mode in {"I", "F"} or source.mode.startswith("I;16"):
                    raise ImageInputError("不支持超过 8 位的图片")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    return Image.alpha_composite(background, rgba).convert("RGB")
                return oriented.convert("RGB")
    except ImageInputError:
        raise
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ImageInputError(f"无法读取图片 {path.name}: {exc}") from exc


def resize_image(image: Image.Image, max_side: int) -> Image.Image:
    if max_side < 0:
        raise ValueError("max_side 必须为 0 或正整数")
    if max_side == 0 or max(image.size) <= max_side:
        return image.copy()
    ratio = max_side / max(image.size)
    size = tuple(max(1, round(value * ratio)) for value in image.size)
    return image.resize(size, Image.Resampling.LANCZOS)


def padded_bgr(image: Image.Image) -> np.ndarray:
    """Return contiguous float32 HWC BGR, right/bottom padded to 16."""
    rgb = np.asarray(image, dtype=np.float32) / 255.0
    height, width = rgb.shape[:2]
    mode = "reflect" if min(height, width) > 1 else "edge"
    padded = np.pad(rgb, ((0, -height % 16), (0, -width % 16), (0, 0)), mode=mode)
    return np.ascontiguousarray(padded[:, :, ::-1])


def bgr_to_image(bgr: np.ndarray, size: tuple[int, int]) -> Image.Image:
    width, height = size
    rgb = bgr[:height, :width, ::-1]
    if not np.isfinite(rgb).all():
        raise ValueError("模型输出包含非有限值")
    return Image.fromarray(np.rint(np.clip(rgb, 0, 1) * 255).astype(np.uint8))


def format_file_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.2f} {unit}（{size_bytes:,} 字节）"
        value /= 1024
    raise AssertionError("unreachable")


def save_png_exclusive(image: Image.Image, directory: str | Path, stem: str) -> Path:
    """Atomically reserve a new output name; never replace any existing file."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(100_000):
        suffix = "" if index == 0 else f"_{index}"
        candidate = directory / f"{stem}_clean{suffix}.png"
        try:
            stream = candidate.open("xb")
        except FileExistsError:
            continue
        try:
            with stream:
                image.save(stream, format="PNG")
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
        return candidate
    raise FileExistsError("输出文件名冲突过多，请选择其他输出目录")
