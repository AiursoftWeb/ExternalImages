FROM hub.aiursoft.com/aiursoft/internalimages/anduinos-internal:latest
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY main.py ./
USER 1654:1654
HEALTHCHECK --interval=60s --timeout=10s --start-period=15m --retries=3 CMD ["python3", "main.py", "--healthcheck"]
ENTRYPOINT ["python3", "main.py"]
