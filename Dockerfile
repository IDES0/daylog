FROM python:3.13-slim

# git + openssh-client: vault.py shells out to `git`, and pushes to the
# private daylog-vault repo happen over SSH using a deploy key (see
# docker-entrypoint.sh).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Pre-download the faster-whisper "small" model at build time so the
# container never needs network access to Hugging Face at runtime — only
# Telegram and Anthropic, both required anyway.
RUN uv run python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["docker-entrypoint.sh"]
