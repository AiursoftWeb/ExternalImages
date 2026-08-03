import os
import json
import asyncio
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from loguru import logger
from agent_commands import CODEX_FILE_CREDENTIAL_CONFIG, build_agent_command

app = FastAPI(title="Folder Agent API")

max_parallel = int(os.getenv("MAX_PARALLEL_PROCESSES", "8"))
inference_lock = asyncio.Semaphore(max_parallel)

class AskRequest(BaseModel):
    system_prompt: str
    question: str

def ensure_qwen_config():
    """Ensure qwen configuration exists based on environment variables."""
    if os.getenv("PROVIDER", "qwen").lower() != "qwen":
        return

    qwen_dir = Path.home() / ".qwen"
    qwen_settings = qwen_dir / "settings.json"

    # Read configuration from environment variables
    api_key = os.getenv("QWEN_API_KEY") or os.getenv("OLLAMA_GATEWAY_API_KEY")
    base_url = os.getenv("QWEN_BASE_URL", "https://ollama.aiursoft.com/v1")
    model_name = os.getenv("QWEN_MODEL_NAME", "aiursoft-instruct:latest")

    if api_key:
        qwen_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "security": {
                "auth": {
                    "selectedType": "openai"
                }
            },
            "model": {
                "name": model_name
            },
            "env": {
                "QWEN_CUSTOM_API_KEY": api_key
            },
            "modelProviders": {
                "openai": [
                    {
                        "id": model_name,
                        "name": "Folder Agent Provider",
                        "envKey": "QWEN_CUSTOM_API_KEY",
                        "baseUrl": base_url
                    }
                ]
            }
        }
        with open(qwen_settings, "w") as f:
            json.dump(config, f, indent=2)
        logger.info(f"Configured Qwen with model: {model_name}, base_url: {base_url}")
    else:
        logger.warning("QWEN_API_KEY not found. Qwen might not work unless ~/.qwen/settings.json is manually provided.")

@app.on_event("startup")
async def startup_event():
    ensure_qwen_config()

@app.get("/health")
async def health():
    return {"status": "ok"}


async def ensure_codex_authenticated():
    """Return a service-friendly error until an operator logs Codex in."""
    try:
        process = await asyncio.create_subprocess_exec(
            "codex",
            "-c", CODEX_FILE_CREDENTIAL_CONFIG,
            "login",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
    except FileNotFoundError as exc:
        logger.exception("Codex CLI is not installed")
        raise HTTPException(status_code=500, detail="Codex CLI is not installed") from exc

    if process.returncode != 0:
        logger.warning("Codex is not authenticated")
        raise HTTPException(
            status_code=503,
            detail=(
                "Codex is not authenticated. Run "
                "'codex login --device-auth' as the container user."
            ),
        )

@app.post("/ask")
async def ask(request: AskRequest):
    logger.info(f"Received question: {request.question}")

    # Target directory for the agent to run in
    import_dir = "/import"
    if not os.path.exists(import_dir):
        # Fallback to current directory if /import doesn't exist (local dev)
        import_dir = os.getcwd()

    provider = os.getenv("PROVIDER", "qwen").lower()

    if provider == "codex":
        await ensure_codex_authenticated()

    cmd = build_agent_command(
        provider,
        request.system_prompt,
        request.question,
    )

    async with inference_lock:
        logger.info(f"Starting {provider} inference...")
        try:
            # Use asyncio to run the subprocess without blocking the event loop
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=import_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error(f"{provider} execution failed: {stderr.decode()}")
                raise HTTPException(status_code=500, detail=f"{provider} error: {stderr.decode()}")

            return {"answer": stdout.decode().strip()}
        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Unexpected error during {provider} inference")
            raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
