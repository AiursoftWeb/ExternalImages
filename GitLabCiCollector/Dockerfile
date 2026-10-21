FROM hub.aiursoft.com/aiursoft/internalimages/anduinos-internal:latest
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates postgresql-client \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN install -d -m 0755 /app
WORKDIR /app
COPY --chmod=644 main.py schema.sql ./
USER 1654:1654
HEALTHCHECK --interval=60s --timeout=10s --start-period=15m --retries=3 CMD ["python3", "main.py", "--healthcheck"]
ENTRYPOINT ["python3", "main.py"]
