FROM ghcr.io/astral-sh/uv:0.11.17 AS uv
FROM python:3.12-slim-bookworm AS runtime
COPY --from=uv /uv /bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    INPUT_DIR=/data/input OUTPUT_DIR=/data/output \
    PROCESSING_SLOTS=1 OPENCV_THREADS=2 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 \
    HOME=/data TMPDIR=/data MODEL_WEIGHTS=/app/models/default.h5 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN mkdir -p /data
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra http
COPY image-preprocess/pyproject.toml image-preprocess/uv.lock ./image-preprocess/
COPY image-preprocess/src ./image-preprocess/src
RUN uv sync --project image-preprocess --frozen --no-dev
COPY chart-enhance/pyproject.toml chart-enhance/uv.lock ./chart-enhance/
COPY chart-enhance/src ./chart-enhance/src
RUN uv sync --project chart-enhance --frozen --no-dev
COPY moire-remove/pyproject.toml moire-remove/uv.lock ./moire-remove/
COPY moire-remove/src ./moire-remove/src
COPY docker-install-cpu.py ./
RUN python docker-install-cpu.py
COPY scan.py pipeline.py api.py job_logging.py ./
COPY SOURCE-NOTICES.md ./
RUN mkdir -p /app/models
RUN groupadd --gid 10001 scanner \
    && useradd --uid 10001 --gid scanner --no-create-home scanner \
    && mkdir -p /data/input /data/output \
    && chown -R scanner:scanner /data \
    && chmod -R a+rX /app
USER 10001:10001
EXPOSE 9863
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9863/health', timeout=3)"
CMD ["sh", "-ec", "if ! test -r \"$MODEL_WEIGHTS\" || ! test -s \"$MODEL_WEIGHTS\"; then echo 'Mount the readable scanner checkpoint at MODEL_WEIGHTS.' >&2; exit 1; fi; exec python -m uvicorn api:app --host 0.0.0.0 --port 9863 --workers 1"]

FROM runtime AS test
USER root
COPY tests ./tests
COPY Dockerfile .dockerignore ./
RUN uv sync --frozen --extra http --group test
ENV MODEL_WEIGHTS=/tmp/scanner-test.h5
RUN --network=none moire-remove/.venv/bin/python tests/make_identity_checkpoint.py "$MODEL_WEIGHTS" \
    && python -m pytest -q tests

FROM runtime AS release
