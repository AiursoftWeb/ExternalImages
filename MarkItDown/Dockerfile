FROM hub.aiursoft.com/aiursoft/internalimages/anduinos-internal:latest

# 系统依赖安装
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates python3 python3-pip python3-dev python3-venv \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# 安装 uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# 创建并使用虚拟环境 (锁定 Python 3.13)
RUN uv python install 3.13
RUN uv venv --python 3.13 /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# 安装 MarkItDown 及常用格式支持
RUN uv pip install \
    "markitdown[pptx,docx,xlsx,pdf]" \
    fastapi \
    uvicorn \
    python-multipart \
    loguru

COPY . .

# 暴露端口
EXPOSE 8000

# 启动服务
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1
