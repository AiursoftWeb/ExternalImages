FROM hub.aiursoft.com/aiursoft/internalimages/anduinos-internal:latest

# Configure environment variables
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    NODE_MAJOR=24

# Install basic dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    gnupg \
    python3 \
    python3-pip \
    python3-venv \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg --yes && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Qwen, Gemini, Claude Code, and Codex CLI tools
RUN npm install -g \
    @qwen-code/qwen-code@latest \
    @google/gemini-cli@latest \
    @anthropic-ai/claude-code@latest \
    @openai/codex@latest && \
    codex --version

# Install Python dependencies
RUN pip3 install --break-system-packages --ignore-installed \
    fastapi \
    uvicorn \
    loguru \
    pydantic

# Create a non-root user for Claude Code (--dangerously-skip-permissions refuses to run as root)
RUN useradd --create-home --shell /bin/bash appuser

# Create directories
RUN mkdir -p /import /app /home/appuser/.codex && \
    chown -R appuser:appuser /import /app /home/appuser/.codex

# Keep Codex configuration and its refreshable ChatGPT login under appuser.
ENV HOME=/home/appuser \
    CODEX_HOME=/home/appuser/.codex

WORKDIR /app
COPY main.py agent_commands.py ./

# Set /import as a volume (suggested usage)
VOLUME /import

# Persist Codex's manually-created ChatGPT login across container restarts.
VOLUME /home/appuser/.codex

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Switch to non-root user for Claude Code compatibility
USER appuser

# Start the application
CMD ["python3", "main.py"]
