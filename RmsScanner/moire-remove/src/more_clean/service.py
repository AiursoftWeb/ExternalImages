"""Lazy, serialized MBCNN inference with explicit device and resize policy."""

from __future__ import annotations

import gc
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import torch
from PIL import Image

from .image_io import bgr_to_image, load_image, padded_bgr, resize_image

DEFAULT_WEIGHTS = Path(__file__).resolve().parents[2] / "weights" / "step-20000.h5"
MAX_RESIZE_RETRIES = 3
logger = logging.getLogger(__name__)


class InferenceMemoryError(RuntimeError):
    """CUDA memory is insufficient; callers must opt in to smaller inference."""


@dataclass(frozen=True)
class DemoireResult:
    original: Image.Image
    output: Image.Image
    original_size: tuple[int, int]
    processed_size: tuple[int, int]
    padded_size: tuple[int, int]
    device: str
    elapsed_seconds: float
    peak_memory_bytes: int | None
    retries: int
    notices: tuple[str, ...]


def resolve_device(device: str) -> torch.device:
    if device not in {"auto", "cpu", "cuda"}:
        raise ValueError("device 必须为 auto、cpu 或 cuda")
    available = torch.cuda.is_available() if device != "cpu" else False
    if device == "cuda" and not available:
        raise ValueError(
            "请求了 CUDA，但 CUDA 不可用；请检查驱动或明确使用 --device cpu"
        )
    return torch.device("cuda" if available else "cpu")


class DemoireService:
    """One lazy model per service; all model loading/inference is serialized.

    Sizes are (width, height). Output stays at processed resolution, never upscaled.
    Timing includes decode, first-use weight loading and any retries. CPU peak memory
    is unavailable (None); CUDA memory reports PyTorch peak allocated device bytes.
    """

    def __init__(
        self,
        weights: str | Path | None = None,
        device: str = "auto",
        cpu_threads: int = 4,
    ):
        if (
            isinstance(cpu_threads, bool)
            or not isinstance(cpu_threads, int)
            or cpu_threads < 1
        ):
            raise ValueError("cpu_threads 必须为正整数")
        self.cpu_threads = cpu_threads
        self.weights = Path(weights) if weights is not None else DEFAULT_WEIGHTS
        self.device = resolve_device(device)
        self.requested_device = device
        self._model = None
        self._lock = threading.Lock()

    def _get_model(self):
        if self._model is None:
            from .checkpoints import load_mbcnn

            self._model = load_mbcnn(self.weights, device=self.device)
        return self._model

    def close(self):
        """Release the cached model before an explicit device change."""
        with self._lock:
            self._model = None
            gc.collect()
            if self.device.type == "cuda":
                torch.cuda.empty_cache()

    def _infer(self, image: Image.Image) -> tuple[Image.Image, tuple[int, int]]:
        array = padded_bgr(image)
        tensor = torch.from_numpy(array).permute(2, 0, 1).unsqueeze(0)
        tensor = tensor.to(device=self.device, dtype=torch.float32)
        with torch.inference_mode():
            predictions = self._get_model()(tensor)
            if not isinstance(predictions, (tuple, list)) or not predictions:
                raise ValueError("模型必须返回 quarter/half/full 输出序列")
            prediction = predictions[-1]
            if prediction.shape != tensor.shape:
                raise ValueError("模型全尺寸输出形状与输入不匹配")
            output = prediction[0].detach().to("cpu", dtype=torch.float32)
            output = output.permute(1, 2, 0).numpy()
        return bgr_to_image(output, image.size), (array.shape[1], array.shape[0])

    def process(
        self,
        input_path: str | Path,
        max_side: int = 0,
        auto_resize: bool = False,
    ) -> DemoireResult:
        if isinstance(max_side, bool) or not isinstance(max_side, int) or max_side < 0:
            raise ValueError("max_side 必须为 0 或正整数")
        with self._lock:
            started = time.perf_counter()
            original = load_image(input_path)
            image = resize_image(original, max_side)
            notices: list[str] = []
            retries = 0
            cuda = self.device.type == "cuda"
            if not cuda:
                torch.set_num_threads(self.cpu_threads)
                notices.append(
                    f"CPU 使用 {self.cpu_threads} 个计算线程；原尺寸推理可能占用大量系统内存。"
                )
            if cuda:
                torch.cuda.synchronize(self.device)
                torch.cuda.reset_peak_memory_stats(self.device)
            while True:
                oom_message = None
                try:
                    output, padded_size = self._infer(image)
                    if cuda:
                        torch.cuda.synchronize(self.device)
                    break
                except torch.cuda.OutOfMemoryError as exc:
                    if not cuda:
                        raise
                    oom_message = str(exc)
                # Exception tracebacks retain the failed forward's CUDA tensors.
                # Leave the except scope before releasing the allocator cache.
                gc.collect()
                torch.cuda.empty_cache()
                if (
                    not auto_resize
                    or retries >= MAX_RESIZE_RETRIES
                    or max(image.size) <= 64
                ):
                    raise InferenceMemoryError(
                        f"CUDA 显存不足（尝试 {image.width}×{image.height}）。"
                        "保留原尺寸请手动选择 --device cpu（较慢且占系统内存）；"
                        "也可主动设置 --max-side 或启用 --auto-resize 缩小重试。"
                        "不会自动切换 CPU。"
                        + (f" 详情：{oom_message}" if oom_message else "")
                    ) from None
                retries += 1
                limit = max(64, int(max(image.size) * 0.75))
                image = resize_image(original, limit)
                notice = (
                    f"CUDA 显存不足，自动缩小重试 {retries}/{MAX_RESIZE_RETRIES}: "
                    f"{image.width}×{image.height}"
                )
                notices.append(notice)
                logger.warning(notice)
            if image.size != original.size:
                notices.append(
                    f"图片已从 {original.width}×{original.height} 缩至 "
                    f"{image.width}×{image.height}，细节会减少；如需保留原尺寸，"
                    "将最长边设为 0（CLI: --max-side 0），并关闭自动缩小。"
                )
            peak = torch.cuda.max_memory_allocated(self.device) if cuda else None
            return DemoireResult(
                original=original,
                output=output,
                original_size=original.size,
                processed_size=output.size,
                padded_size=padded_size,
                device=str(self.device),
                elapsed_seconds=time.perf_counter() - started,
                peak_memory_bytes=peak,
                retries=retries,
                notices=tuple(notices),
            )
