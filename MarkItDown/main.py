#!/usr/bin/env python3
"""
MarkItDown – Web-based file-to-Markdown conversion service powered by Microsoft MarkItDown.
"""

import os
import time
import secrets
import logging
import platform
import tempfile
from typing import Optional
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse
from markitdown import MarkItDown
from loguru import logger

# ── Configuration ────────────────────────────────────────────────────────────
def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None: return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

MARKDOWN_ACCESS_TOKEN = os.getenv("MARKDOWN_ACCESS_TOKEN", "").strip()
AUTH_REQUIRED = bool(MARKDOWN_ACCESS_TOKEN)
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# ── Application ──────────────────────────────────────────────────────────────
md_converter: Optional[MarkItDown] = None
system_info: dict = {}

def get_system_info() -> dict:
    return {
        "platform": platform.platform(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }

@asynccontextmanager
async def lifespan(app: FastAPI):
    global md_converter, system_info
    system_info = get_system_info()

    try:
        md_converter = MarkItDown()
        logger.info("MarkItDown converter initialized successfully")
    except Exception as e:
        logger.error("MarkItDown initialization failed: %s", e)
        md_converter = None
    yield

app = FastAPI(title="MarkItDown-API", lifespan=lifespan)

def _is_authorized_bearer(authorization: str | None) -> bool:
    if not AUTH_REQUIRED: return True
    if not authorization: return False
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer": return False
    return secrets.compare_digest(token, MARKDOWN_ACCESS_TOKEN)

@app.middleware("http")
async def access_token_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/") and not _is_authorized_bearer(request.headers.get("Authorization")):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)

@app.get("/", response_class=HTMLResponse)
def web_portal():
    html_path = Path(__file__).with_name("index.html")
    html = html_path.read_text(encoding="utf-8") if html_path.exists() else "<h1>MarkItDown API</h1>"
    auth_flag_script = f"<script>const __MD_AUTH_REQUIRED__ = {'true' if AUTH_REQUIRED else 'false'};</script>"
    return html.replace("</head>", f"{auth_flag_script}</head>", 1)

@app.get("/api/system")
def api_system():
    return {
        **system_info,
        "model_loaded": md_converter is not None,
        "auth_required": AUTH_REQUIRED,
        "max_file_size_mb": MAX_FILE_SIZE_MB,
    }

@app.post("/api/convert")
async def convert_to_markdown(file: UploadFile = File(...)):
    """Convert an uploaded file to Markdown."""
    if md_converter is None:
        raise HTTPException(503, "MarkItDown converter is not loaded.")

    if not file.filename:
        raise HTTPException(400, "No file provided.")

    logger.info("Convert request received: filename=%s content_type=%s", file.filename, file.content_type)

    try:
        contents = await file.read()
        logger.info("File read: filename=%s size=%d bytes", file.filename, len(contents))

        if len(contents) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(400, f"File exceeds maximum size of {MAX_FILE_SIZE_MB} MB.")

        # Write to temp file (markitdown works with file paths)
        suffix = Path(file.filename).suffix or ""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name

        try:
            start_time = time.perf_counter()
            result = md_converter.convert(tmp_path)
            duration = time.perf_counter() - start_time

            markdown_text = result.text_content

            logger.info(
                "Convert OK: filename=%s dur=%.3fs input_bytes=%d output_chars=%d",
                file.filename, duration, len(contents), len(markdown_text),
            )

            return {
                "status": "ok",
                "filename": file.filename,
                "duration_s": round(duration, 3),
                "input_bytes": len(contents),
                "output_chars": len(markdown_text),
                "markdown": markdown_text,
            }
        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Convert failed: %s", e, exc_info=True)
        raise HTTPException(500, f"Conversion failed: {e}")

@app.get("/health")
def health_check():
    return {"status": "ok" if md_converter is not None else "degraded"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
