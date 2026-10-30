FROM hub.aiursoft.com/aiursoft/internalimages/nvidia:latest

ARG SINGLE_MODEL=true
ARG USE_PYPI_MIRROR=false
ENV SINGLE_MODEL=${SINGLE_MODEL}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    WHISPERX_MODEL=large-v3 \
    WHISPERX_DEVICE=cuda \
    WHISPERX_COMPUTE_TYPE=float16 \
    CUDA_VISIBLE_DEVICES=0 \
    HF_HOME=/root/.cache/huggingface \
    LD_LIBRARY_PATH=/usr/local/lib/python3.12/dist-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/dist-packages/nvidia/cudnn/lib:$LD_LIBRARY_PATH

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN if [ "$USE_PYPI_MIRROR" = "true" ]; then \
        PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple; \
    else \
        PIP_INDEX_URL=https://pypi.org/simple; \
    fi \
    && pip install --index-url "$PIP_INDEX_URL" \
        "fastapi==0.116.1" \
        "uvicorn[standard]==0.35.0" \
        "python-multipart==0.0.20" \
        "faster-whisper==1.2.1" \
        "nvidia-cublas-cu12==12.8.4.1" \
        "nvidia-cudnn-cu12==9.10.2.21"

# Bake the curated model levels (models_config.BAKED_MODELS) into the image
# at build time so ops can switch offline. The cached weights are device-agnostic
# and app.py reloads them onto the GPU (WHISPERX_DEVICE=cuda) at runtime.
COPY models_config.py bake_models.py /app/
RUN python bake_models.py

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY app.py /app/app.py

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
    CMD python -c "import os, urllib.request; request = urllib.request.Request('http://127.0.0.1:8000/health', headers={'Authorization': 'Bearer ' + os.environ['ASR_WHISPERX_TOKEN']}); urllib.request.urlopen(request, timeout=3).read()" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
