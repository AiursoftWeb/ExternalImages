import os
from faster_whisper import download_model
from models_config import BAKED_MODELS

# Bake the curated faster-whisper model levels (models_config.BAKED_MODELS) into the
# image at build time so ops can switch offline. Edit BAKED_MODELS and rebuild
# to change the offline-available set.
# The cached weights are device-agnostic and app.py reloads them onto the GPU
# (WHISPERX_DEVICE=cuda) at runtime.
download_root = os.getenv("HF_HOME", "/root/.cache/huggingface")
os.makedirs(download_root, exist_ok=True)
for name in BAKED_MODELS:
    print(f"Baking faster-whisper model '{name}' into '{download_root}'...")
    download_model(name, cache_dir=download_root)
print("Model baking complete.")
