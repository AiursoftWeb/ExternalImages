# Folder Agent

Folder Agent is a containerized REST API wrapper that supports multiple AI agent CLIs ([Qwen Code](https://github.com/QwenLM/qwen-code), [Gemini CLI](https://github.com/google-gemini/gemini-cli), [Claude Code](https://github.com/anthropics/claude-code), and [OpenAI Codex](https://developers.openai.com/codex/cli)). It allows you to query a knowledge base (like a directory of Markdown files) via a simple HTTP POST request.

## Features

- **Multi-Provider**: Supports Qwen, Gemini, Claude Code, and Codex as agent engines.
- **Agentic RAG**: Uses the agent's built-in reasoning to search and read your files.
- **REST API**: Simple FastAPI backend to integrate with your existing systems.
- **YOLO Mode**: Runs without user intervention, perfect for automated backends.
- **Dynamic Configuration**: Automatically configures the provider based on environment variables.

## How to Run

### Build the Image

```bash
docker build -t folder-agent .
```

### Run the Container (Example)

```bash
docker run -d \
  -p 8000:8000 \
  -v /path/to/your/markdowns:/import \
  -e QWEN_API_KEY=your_api_key_here \
  --name folder-agent \
  folder-agent
```

## Multi-Provider Support

This project supports multiple agent engines. The default is **Qwen**, ensuring full backward compatibility.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PROVIDER` | The agent engine to use. Supported: `qwen`, `gemini`, `claude`, `codex`. | **`qwen`** |
| `CODEX_MODEL` | Optional model passed to `codex exec --model`. If unset, Codex uses its configured or recommended model. | (Managed by Codex) |
| `CLAUDE_MODEL` | The model name for Claude Code (optional). | (Managed by Claude Code) |
| `ANTHROPIC_API_KEY` | Your Anthropic API key for Claude Code. | (Required for claude) |
| `ANTHROPIC_BASE_URL` | The base URL for Anthropic-compatible API (optional). | `https://api.anthropic.com` |
| `GEMINI_MODEL` | The model name for Gemini (optional). | (Managed by Gemini CLI) |
| `QWEN_API_KEY` | Your AI provider API Key for Qwen. | (Required for qwen) |
| `QWEN_BASE_URL` | The base URL for Qwen's OpenAI-compatible API. | `https://ollama.aiursoft.com/v1` |
| `QWEN_MODEL_NAME` | The name of the Qwen model to use. | `aiursoft-instruct:latest` |
| `OLLAMA_GATEWAY_API_KEY` | Legacy alias for `QWEN_API_KEY`. | - |

### Backward Compatibility

This project is fully backward compatible. If the `PROVIDER` environment variable is not set, it defaults to `qwen`, and all existing Qwen-related configurations will work exactly as before.

### Switching to Codex

Codex can use a ChatGPT account, so an API key is not required. Because the container is headless, use Codex's device-code login and persist its writable home directory in a Docker volume.

Create the volume and start Folder Agent first. The service intentionally starts before authentication so that you can log in with `docker exec`:

```bash
docker volume create folder-agent-codex-home

docker run -d \
  -p 8000:8000 \
  -v /path/to/your/markdowns:/import:ro \
  -v folder-agent-codex-home:/home/appuser/.codex \
  -e PROVIDER=codex \
  --name folder-agent \
  folder-agent
```

Start device-code authentication as the same non-root user that runs the API:

```bash
docker exec -it --user appuser folder-agent \
  codex login \
  -c 'cli_auth_credentials_store="file"' \
  --device-auth
```

Open the URL shown by Codex in a browser, sign in with ChatGPT, and enter the one-time code. Device-code login must be enabled in the ChatGPT account's security settings or by the ChatGPT workspace administrator.

Verify the stored login:

```bash
docker exec --user appuser folder-agent \
  codex -c 'cli_auth_credentials_store="file"' login status
```

Codex refreshes its ChatGPT tokens during use. Keep the volume writable and reuse it when recreating the container. Treat its `auth.json` as a password: never commit it, print it, or share it.

To select a model for every request handled by the container, set `CODEX_MODEL`:

```bash
docker run -d \
  -p 8000:8000 \
  -v /path/to/your/markdowns:/import:ro \
  -v folder-agent-codex-home:/home/appuser/.codex \
  -e PROVIDER=codex \
  -e CODEX_MODEL=gpt-5.6-terra \
  --name folder-agent \
  folder-agent
```

The selected model must be available to the signed-in ChatGPT account and workspace. If `CODEX_MODEL` is unset, Codex uses the model in its persisted `config.toml`, or its recommended default when no model is configured.

Folder Agent runs `codex exec` with its non-interactive YOLO option. Keep the API private and mount untrusted or read-only knowledge bases with `:ro`.

### Switching to Claude Code

To use the Claude Code provider:

1. **Prepare API credentials**: You need an Anthropic API key, or an Anthropic-compatible endpoint (e.g., Aiursoft's proxy, DeepSeek).
2. **Set environment variables**: Configure `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` (if using a custom endpoint), and `CLAUDE_MODEL`.

**Example 1: Using Aiursoft's proxy with a custom model**

```bash
docker run -d \
  -p 8000:8000 \
  -v /path/to/your/markdowns:/import \
  -e PROVIDER=claude \
  -e ANTHROPIC_API_KEY=your_api_key_here \
  -e ANTHROPIC_BASE_URL=https://ollama.aiursoft.com \
  -e CLAUDE_MODEL=aiursoft-super:latest \
  --name folder-agent \
  folder-agent
```

**Example 2: Using DeepSeek via Anthropic-compatible API**

```bash
docker run -d \
  -p 8000:8000 \
  -v /path/to/your/markdowns:/import \
  -e PROVIDER=claude \
  -e ANTHROPIC_API_KEY=sk-your-deepseek-key \
  -e ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic \
  -e CLAUDE_MODEL=deepseek-v4-pro \
  --name folder-agent \
  folder-agent
```

**Example 3: Using official Anthropic API**

```bash
docker run -d \
  -p 8000:8000 \
  -v /path/to/your/markdowns:/import \
  -e PROVIDER=claude \
  -e ANTHROPIC_API_KEY=sk-ant-your-key \
  -e CLAUDE_MODEL=claude-sonnet-4-6 \
  --name folder-agent \
  folder-agent
```

### Switching to Gemini

To use the Gemini provider:

1. **Authenticate**: Run `gemini login` on your host machine.
2. **Mount Credentials**: Mount `~/.gemini` to `/home/appuser/.gemini` in the container.
3. **Set Provider**: Set environment variable `PROVIDER=gemini`.

```bash
docker run -d \
  -p 8000:8000 \
  -v /path/to/your/markdowns:/import \
  -v ~/.gemini:/home/appuser/.gemini \
  -e PROVIDER=gemini \
  --name folder-agent \
  folder-agent
```

## API Usage

### Health Check

`GET /health`

### Ask a Question

`POST /ask`

**Payload:**

```json
{
  "system_prompt": "You are a helpful assistant. Use the files in the current directory to answer the question.",
  "question": "What is the main topic of my recent blog posts?"
}
```

**Response:**

```json
{
  "answer": "Based on the files in the directory, your recent blog posts focus on..."
}
```

## Integration Example (Python)

```python
import requests

response = requests.post(
    "http://localhost:8000/ask",
    json={
        "system_prompt": "You are Anduin's Cyber-Twin.",
        "question": "What do you think about AI agents?"
    }
)

print(response.json()["answer"])
```
