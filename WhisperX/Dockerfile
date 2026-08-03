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
    HF_HOME=/root/.cache/huggingface

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN aria2c --continue=true --max-connection-per-server=16 --max-tries=10 --min-split-size=1M --retry-wait=5 --split=16 \
        --dir=/tmp --out=torch-2.6.0+cu124-cp312-cp312-linux_x86_64.whl \
        "https://download-r2.pytorch.org/whl/cu124/torch-2.6.0%2Bcu124-cp312-cp312-linux_x86_64.whl" \
    && echo "a393b506844035c0dac2f30ea8478c343b8e95a429f06f3b3cadfc7f53adb597  /tmp/torch-2.6.0+cu124-cp312-cp312-linux_x86_64.whl" | sha256sum --check \
    && aria2c --continue=true --max-connection-per-server=16 --max-tries=10 --min-split-size=1M --retry-wait=5 --split=16 \
        --dir=/tmp --out=torchaudio-2.6.0+cu124-cp312-cp312-linux_x86_64.whl \
        "https://download-r2.pytorch.org/whl/cu124/torchaudio-2.6.0%2Bcu124-cp312-cp312-linux_x86_64.whl" \
    && echo "3e5ffa69606171c74f3e2b969785ead50b782ca657e746aaee1ee7cc88dcfc08  /tmp/torchaudio-2.6.0+cu124-cp312-cp312-linux_x86_64.whl" | sha256sum --check \
    && pip install --timeout 600 --retries 10 --index-url https://download.pytorch.org/whl/cu124 \
        /tmp/torch-2.6.0+cu124-cp312-cp312-linux_x86_64.whl \
        /tmp/torchaudio-2.6.0+cu124-cp312-cp312-linux_x86_64.whl \
    && rm /tmp/torch-2.6.0+cu124-cp312-cp312-linux_x86_64.whl \
        /tmp/torchaudio-2.6.0+cu124-cp312-cp312-linux_x86_64.whl
RUN if [ "$USE_PYPI_MIRROR" = "true" ]; then \
        PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple; \
    else \
        PIP_INDEX_URL=https://pypi.org/simple; \
    fi \
    && pip install --index-url "$PIP_INDEX_URL" "fastapi==0.116.1" "uvicorn[standard]==0.35.0" "python-multipart==0.0.20" "whisperx==3.8.6"

# Bake the curated WhisperX model levels (models_config.BAKED_MODELS) into the
# image at build time so ops can switch offline. Edit BAKED_MODELS and rebuild
# to change the offline-available set. device='cpu' only triggers the download;
# the cached weights are device-agnostic and app.py reloads them onto the GPU
# (WHISPERX_DEVICE=cuda) at runtime. The default is large-v3, which fits
# comfortably on one RTX 3090 (24G) leaving headroom for other services.
COPY models_config.py bake_models.py /app/
RUN python bake_models.py

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY app.py /app/app.py

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 CMD python -c "import os, urllib.request; request = urllib.request.Request('http://127.0.0.1:8000/health', headers={'Authorization': 'Bearer ' + os.environ['ASR_WHISPERX_TOKEN']}); urllib.request.urlopen(request, timeout=3).read()" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
