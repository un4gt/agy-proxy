FROM python:3.12-slim

# Runtime deps for agy (git for workspace detection, curl/ca-certs for install & OAuth)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git bash procps \
    && rm -rf /var/lib/apt/lists/*

# Pin the official artifact instead of running the moving latest installer.
ARG AGY_VERSION=1.4.2
ARG AGY_URL=https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.2-4835981233356800/linux-x64/cli_linux_x64.tar.gz
ARG AGY_SHA512=4a393afc1369cee94e817c1b430c9128d02b12ebd4d5703ace5cd47b2333e216d673fb0097cbf7195f757b110163ba9850e9eea3fdbbaddd4b25668b82c03f05
RUN curl -fsSL "$AGY_URL" -o /tmp/agy.tar.gz \
    && echo "$AGY_SHA512  /tmp/agy.tar.gz" | sha512sum -c - \
    && tar -xzf /tmp/agy.tar.gz -C /tmp antigravity \
    && mkdir -p /root/.local/bin \
    && mv /tmp/antigravity /root/.local/bin/agy \
    && chmod +x /root/.local/bin/agy \
    && test "$(/root/.local/bin/agy --version)" = "$AGY_VERSION" \
    && rm /tmp/agy.tar.gz

ENV PATH="/root/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY main.py ./

RUN pip install --no-cache-dir .

# Only the active observer modules ship; historical harnesses stay in source.
COPY tools/agy_native_wire_probe/runtime.py tools/agy_native_wire_probe/probe_gdb.py tools/agy_native_wire_probe/observe.py tools/agy_native_wire_probe/input_clean.py tools/agy_native_wire_probe/symbols.py /app/tools/agy_native_wire_probe/

# Neutral cwd so no workspace rules leak into chat sessions
RUN mkdir -p /workspace-empty
ENV AGY_PROXY_CWD=/workspace-empty \
    AGY_PROXY_HOST=0.0.0.0 \
    AGY_PROXY_DATA_DIR=/root/.agy-proxy

LABEL org.opencontainers.image.title="agy-proxy" \
      org.opencontainers.image.description="Gemini / OpenAI compatible reverse proxy backed by the Antigravity CLI (agy) stream-json duplex mode" \
      org.opencontainers.image.version="0.0.17" \
      org.opencontainers.image.source="https://github.com/un4gt/agy-proxy"

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8317/health', timeout=3)"]

EXPOSE 8317
CMD ["python", "main.py"]
