"""Bounded image decoding, orientation and non-overwriting PNG output."""

import warnings
from pathlib import Path

from PIL import Image, ImageOps

SUPPORTED = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
MAX_PIXELS = 40_000_000
MAX_BYTES = 100 * 1024 * 1024


def preprocess(source: str | Path, output_dir: str | Path, max_side: int = 2048) -> dict:
    """Return a JSON-compatible result; preserve source and all existing outputs."""
    source, output_dir = Path(source), Path(output_dir)
    if isinstance(max_side, bool) or not isinstance(max_side, int) or max_side < 1:
        raise ValueError("max_side 必须是正整数")
    if source.suffix.lower() not in SUPPORTED:
        raise ValueError("不支持该图片扩展名")
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("图片文件超过 100 MiB")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(source) as original:
            if original.width * original.height > MAX_PIXELS:
                raise ValueError("图片超过 4000 万像素")
            if getattr(original, "n_frames", 1) != 1:
                raise ValueError("不支持动画或多页图片")
            if original.mode in {"I", "F"} or original.mode.startswith("I;16"):
                raise ValueError("请先将高位深图片转换为 8 位")
            # Pillow can silently decode 16-bit RGB PNG as 8-bit RGB.
            with source.open("rb") as handle:
                header = handle.read(25)
            if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) == 25 and header[24] > 8:
                raise ValueError("不支持超过 8 位的 PNG")
            oriented = ImageOps.exif_transpose(original)
            original_size = oriented.size
            if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                image = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba).convert(
                    "RGB"
                )
            else:
                image = oriented.convert("RGB")
    if max(image.size) > max_side:
        scale = max_side / max(image.size)
        size = tuple(max(1, round(v * scale)) for v in image.size)
        image = image.resize(size, Image.Resampling.LANCZOS)
    output_dir.mkdir(parents=True, exist_ok=True)
    for number in range(100_000):
        suffix = "" if number == 0 else f"_{number}"
        output = output_dir / f"{source.stem}{suffix}.png"
        try:
            handle = output.open("xb")
        except FileExistsError:
            continue
        try:
            with handle:
                # Do not forward source EXIF/ICC metadata into downstream stages.
                clean = Image.frombytes("RGB", image.size, image.tobytes())
                clean.save(handle, format="PNG")
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        break
    else:
        raise FileExistsError("输出文件重名过多")
    return {
        "input": str(source.resolve()),
        "output": str(output.resolve()),
        "original_size": list(original_size),
        "output_size": list(image.size),
        "input_bytes": source.stat().st_size,
        "output_bytes": output.stat().st_size,
        "resized": image.size != original_size,
        "format": "PNG",
    }
