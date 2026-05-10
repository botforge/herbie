# syntax=docker/dockerfile:1.6
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XDG_CACHE_HOME=/data/whisper-cache

# 1. System deps:
#    1A. ffmpeg — faster-whisper invokes it for audio decode
#    1B. libpq — psycopg's runtime requirement
#    1C. ca-certs — TLS for OpenRouter
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libpq5 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# 2. Pre-download the faster-whisper base model so the first request
#    on a fresh container does not stall. Cached under XDG_CACHE_HOME
#    which is the volume path, so subsequent deploys reuse it.
RUN python -c "from faster_whisper import WhisperModel; \
WhisperModel('base', device='cpu', compute_type='int8', \
download_root='/data/whisper-cache')" || true

COPY . .

# Default command runs the web process; fly.toml overrides for the
# telegram process.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
