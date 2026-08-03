import os


def baked_models(single_model):
    if (single_model or "true").lower() == "true":
        return ["large-v3"]
    return ["small", "medium", "large-v3"]


BAKED_MODELS = baked_models(os.getenv("SINGLE_MODEL"))


def ensure_baked_model(name):
    if name not in BAKED_MODELS:
        allowed_models = ", ".join(BAKED_MODELS)
        raise ValueError(f"model '{name}' is not baked into this image; allowed models: {allowed_models}")
