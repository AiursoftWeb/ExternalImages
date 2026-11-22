"""Synchronous, bounded HTTP adapter for the existing CPU scanner."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import threading
import warnings
import time
import job_logging as events
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import cv2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.formparsers import MultiPartParser

from scan import process

logger = logging.getLogger(__name__)
FORM_OVERHEAD = 64 * 1024
FORMATS = {"JPEG", "PNG", "TIFF", "WEBP", "BMP"}


@dataclass
class Settings:
    input_dir: Path = Path("/data/input")
    output_dir: Path = Path("/data/output")
    processing_slots: int = 1
    max_file_bytes: int = 50 * 1024 * 1024
    max_pixels: int = 25_000_000
    opencv_threads: int = 2

    @classmethod
    def from_env(cls):
        return cls(
            input_dir=Path(os.getenv("INPUT_DIR", "/data/input")),
            output_dir=Path(os.getenv("OUTPUT_DIR", "/data/output")),
            processing_slots=int(os.getenv("PROCESSING_SLOTS", "1")),
            max_file_bytes=int(os.getenv("MAX_FILE_BYTES", str(50 * 1024 * 1024))),
            max_pixels=int(os.getenv("MAX_PIXELS", "25000000")),
            opencv_threads=int(os.getenv("OPENCV_THREADS", "2")),
        )


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strength: float = Field(default=1.0, gt=0, le=3, allow_inf_nan=False)
    max_side: int = Field(default=2400, ge=128, le=4096)
    no_demoire: bool = False
    passes: int = Field(default=2, ge=1, le=2)


class PathRequest(Options):
    input_path: str = Field(min_length=1)


async def offload(function, *args):
    # A disconnected/cancelled caller must not free a slot while CPU work continues.
    cancellation = threading.Event()
    def work():
        with events.cancellation(cancellation):
            return function(*args)
    task = asyncio.create_task(asyncio.to_thread(work))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            cancellation.set()
            if task.done():
                try:
                    result = task.result()
                except Exception:
                    raise asyncio.CancelledError from None
                break
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        if function is run_job and isinstance(result, dict):
            with events.context(requestId=result["request_id"]):
                shutil.rmtree(args[0].output_dir / result["request_id"], ignore_errors=True)
                events.emit("job", "cancelled", reason="Caller cancelled after processing")
                events.emit("cleanup", "ok")
        raise asyncio.CancelledError
    return result


class AdmissionMiddleware:
    def __init__(self, app, settings):
        self.app = app
        self.settings = settings
        self.slots = threading.BoundedSemaphore(settings.processing_slots)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in {"/process", "/process/upload"}:
            await self.app(scope, receive, send)
            return
        if not self.slots.acquire(blocking=False):
            await JSONResponse({"detail": "Scanner busy; retry later"}, 503, headers={"Retry-After": "1"})(scope, receive, send)
            return
        total = 0
        limit = self.settings.max_file_bytes + FORM_OVERHEAD if scope["path"].endswith("upload") else FORM_OVERHEAD

        async def limited_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > limit:
                raise HTTPException(413, "Request body too large")
            return message

        try:
            await self.app(scope, limited_receive, send)
        finally:
            self.slots.release()


def secure_open(root, relative):
    """Open beneath a trusted root without following replaced path components."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        result = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(result).st_mode):
            os.close(result)
            raise HTTPException(422, "Input must be a regular file")
        return os.fdopen(result, "rb")
    finally:
        os.close(fd)


def validate_image(path, settings):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.width * image.height > settings.max_pixels:
                    raise HTTPException(413, "Image pixel limit exceeded")
                if image.format not in FORMATS:
                    raise HTTPException(422, "Unsupported image type")
                image.verify()
            with Image.open(path) as image:
                image.load()
                return image.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(413, "Image pixel limit exceeded") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise HTTPException(422, "Invalid or undecodable image") from None


def run_job(settings, options, input_path=None, upload=None):
    request_id = uuid4().hex
    name = str(input_path) if input_path is not None else getattr(upload, "filename", None)
    with events.context(requestId=request_id, inputName=name):
        return _run_job(settings, options, request_id, input_path, upload)


def _run_job(settings, options, request_id, input_path=None, upload=None):
    started = time.monotonic()
    events.emit("receipt", "ok", started=started)
    job = settings.output_dir / request_id
    scratch = settings.output_dir / (".upload-" + request_id)
    success = False
    scratch_created = False
    job_created = False
    try:
        scratch.mkdir(mode=0o700)
        scratch_created = True
        if input_path is not None:
            candidate = Path(input_path)
            try:
                source = (candidate if candidate.is_absolute() else settings.input_dir / candidate).resolve()
                relative = source.relative_to(settings.input_dir)
            except (ValueError, RuntimeError, OSError):
                raise HTTPException(422, "Input path must remain inside INPUT_DIR") from None
            if not relative.parts:
                raise HTTPException(422, "Input must be a regular file")
            try:
                stream = secure_open(settings.input_dir, relative)
            except FileNotFoundError:
                raise HTTPException(404, "Input file not found") from None
            except OSError:
                raise HTTPException(422, "Input path cannot be safely opened") from None
            name = source.stem
            reported_input = str(source)
        else:
            stream = upload.file
            name = "image"
            reported_input = None
        snapshot = scratch / (name + ".image")
        try:
            with snapshot.open("xb") as destination:
                total = 0
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > settings.max_file_bytes:
                        raise HTTPException(413, "Image file size limit exceeded")
                    destination.write(chunk)
        finally:
            if input_path is not None:
                stream.close()
        events.enrich(sizeBytes=total)
        events.emit("validation", "begin", started=started)
        width, height = validate_image(snapshot, settings)
        events.enrich(width=width, height=height)
        events.emit("validation", "ok", started=started)
        job.mkdir(mode=0o750)
        job_created = True
        args = SimpleNamespace(**options.model_dump(), no_crop=False, corners=None, debug=False, threads=settings.opencv_threads)
        events.check_cancelled()
        diagnostic = process(snapshot, job, args)
        events.check_cancelled()
        diagnostic["input"] = reported_input
        result = {
            "request_id": request_id,
            "input_path": reported_input,
            "output_path": str(job / (name + ".png")),
            "download_url": f"/results/{request_id}/image",
            "diagnostic": diagnostic,
        }
        (job / ".result.json").write_text(json.dumps({"output_path": result["output_path"]}, ensure_ascii=False))
        events.check_cancelled()
        success = True
        events.emit("job", "ok", output=result["output_path"], started=started)
        return result
    except HTTPException as exc:
        events.emit("validation", "fail", reason=exc.detail, started=started)
        events.emit("job", "fail", reason=exc.detail, started=started)
        raise
    except (events.JobCancelled, KeyboardInterrupt):
        events.emit("job", "cancelled", started=started, reason="Caller cancelled")
        raise
    except Exception as exc:
        events.emit("job", "fail", reason=exc, started=started)
        raise HTTPException(500, "Image processing or storage failed") from None
    finally:
        if scratch_created:
            shutil.rmtree(scratch, ignore_errors=True)
        if job_created and not success:
            shutil.rmtree(job, ignore_errors=True)
        events.emit("cleanup", "ok" if not scratch.exists() and (success or not job_created or not job.exists()) else "fail", started=started)


def read_result(settings, request_id):
    try:
        if UUID(request_id).hex != request_id:
            raise ValueError
        with secure_open(settings.output_dir, Path(request_id) / ".result.json") as stream:
            result = json.load(stream)
        path = Path(result["output_path"])
        relative = path.relative_to(settings.output_dir)
        if relative.parent != Path(request_id) or path.suffix != ".png":
            raise ValueError
        with secure_open(settings.output_dir, relative) as stream:
            return stream.read(), path.name
    except (OSError, ValueError, KeyError, HTTPException):
        raise HTTPException(404, "Result not found") from None


def create_app(settings=None):
    settings = settings or Settings.from_env()
    if min(settings.processing_slots, settings.max_file_bytes, settings.max_pixels, settings.opencv_threads) < 1:
        raise ValueError("Capacity, size limits and thread count must be positive")
    settings.input_dir = settings.input_dir.resolve()
    settings.output_dir = settings.output_dir.resolve()

    @asynccontextmanager
    async def lifespan(app):
        settings.input_dir.mkdir(parents=True, exist_ok=True)
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        cv2.setNumThreads(settings.opencv_threads)
        cv2.ocl.setUseOpenCL(False)
        # Bound the complete request body before parsing; never spill uploads to /tmp.
        MultiPartParser.spool_max_size = settings.max_file_bytes + FORM_OVERHEAD
        yield

    app = FastAPI(title="CPU chart scanner", lifespan=lifespan)
    app.add_middleware(AdmissionMiddleware, settings=settings)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/process")
    async def process_path(body: PathRequest):
        options = Options(**body.model_dump(exclude={"input_path"}))
        return await offload(run_job, settings, options, body.input_path)

    @app.post("/process/upload")
    async def process_upload(request: Request):
        async with request.form(max_files=1, max_fields=len(Options.model_fields) + 1, max_part_size=FORM_OVERHEAD) as form:
            image = form.get("image")
            if image is None or not hasattr(image, "file"):
                raise HTTPException(422, "Provide an image file in field 'image'")
            if len(form.multi_items()) != len(form):
                raise HTTPException(422, "Duplicate form fields are not allowed")
            response_type = form.get("response_type", "json")
            if response_type not in {"json", "image"}:
                raise HTTPException(422, "response_type must be json or image")
            try:
                options = Options(**{k: v for k, v in form.items() if k not in {"image", "response_type"}})
            except ValidationError as error:
                raise HTTPException(422, "Invalid processing parameters") from error
            result = await offload(run_job, settings, options, None, image)
            if response_type == "image":
                content, name = await offload(read_result, settings, result["request_id"])
                return Response(content, media_type="image/png", headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Request-ID": result["request_id"]})
            return result

    @app.get("/results/{request_id}/image")
    async def download(request_id: str):
        content, name = await offload(read_result, settings, request_id)
        # RFC 5987 handles input stems containing quotes or non-ASCII characters.
        from urllib.parse import quote
        return Response(content, media_type="image/png", headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})

    return app


app = create_app()
