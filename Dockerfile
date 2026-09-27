FROM python:3.12-slim

# Runtime deps for agy (git for workspace detection, curl/ca-certs for install & OAuth)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git bash procps \
    && rm -rf /var/lib/apt/lists/*

# Install the Antigravity CLI (agy)
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash

ENV PATH="/root/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY main.py ./

RUN pip install --no-cache-dir .

# Neutral cwd so no workspace rules leak into chat sessions
RUN mkdir -p /workspace-empty
ENV AGY_PROXY_CWD=/workspace-empty \
    AGY_PROXY_HOST=0.0.0.0 \
    AGY_PROXY_DATA_DIR=/root/.agy-proxy

LABEL org.opencontainers.image.title="agy-proxy" \
      org.opencontainers.image.description="Gemini / OpenAI compatible reverse proxy backed by the Antigravity CLI (agy) stream-json duplex mode" \
      org.opencontainers.image.version="0.0.8" \
      org.opencontainers.image.source="https://github.com/un4gt/agy-proxy"

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8317/health', timeout=3)"]

EXPOSE 8317
CMD ["python", "main.py"]
