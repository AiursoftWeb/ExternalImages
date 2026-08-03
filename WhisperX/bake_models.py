import whisperx

from models_config import BAKED_MODELS

# device='cpu' only triggers the download; the cached weights are
# device-agnostic and reloaded onto the GPU by app.py at runtime.
for name in BAKED_MODELS:
    whisperx.load_model(name, device="cpu")
