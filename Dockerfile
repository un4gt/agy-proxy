# Build the patched 1.4.2 CLI in a separate stage so the unpatched 221 MB
# binary is not copied into the final image as an extra layer.
FROM python:3.12-slim AS agy-patched

ARG AGY_VERSION=1.4.2
ARG AGY_URL=https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.2-4835981233356800/linux-x64/cli_linux_x64.tar.gz
ARG AGY_SHA512=4a393afc1369cee94e817c1b430c9128d02b12ebd4d5703ace5cd47b2333e216d673fb0097cbf7195f757b110163ba9850e9eea3fdbbaddd4b25668b82c03f05
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "$AGY_URL" -o /tmp/agy.tar.gz \
    && echo "$AGY_SHA512  /tmp/agy.tar.gz" | sha512sum -c - \
    && tar -xzf /tmp/agy.tar.gz -C /tmp antigravity \
    && mkdir -p /opt/agy-bridge \
    && mv /tmp/antigravity /opt/agy-bridge/agy \
    && chmod +x /opt/agy-bridge/agy \
    && test "$(/opt/agy-bridge/agy --version)" = "$AGY_VERSION" \
    && rm /tmp/agy.tar.gz

# The patcher is stdlib-only and imports emitter.py from its own directory.
COPY app/bridge /patch/app/bridge
RUN python /patch/app/bridge/patcher.py --source /opt/agy-bridge/agy \
         --dest /opt/agy-bridge/agy \
    && test "$(/opt/agy-bridge/agy --version)" = "1.4.2"


FROM python:3.12-slim

# Runtime deps for agy (git for workspace detection, curl/ca-certs for OAuth)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git bash procps \
    && rm -rf /var/lib/apt/lists/*

COPY --from=agy-patched /opt/agy-bridge/agy /opt/agy-bridge/agy

# Keep the pinned CLI build stable; the bridge also disables /update in-app.
ENV PATH="/opt/agy-bridge:/root/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    AGY_CLI_DISABLE_AUTO_UPDATE=true
WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY main.py ./

RUN pip install --no-cache-dir .

# Only the active observer modules ship; historical harnesses stay in source.
COPY tools/agy_cli_protocol.py /app/tools/agy_cli_protocol.py
COPY tools/agy_native_wire_probe/runtime.py tools/agy_native_wire_probe/probe_gdb.py tools/agy_native_wire_probe/observe.py tools/agy_native_wire_probe/input_clean.py tools/agy_native_wire_probe/input_guard.py tools/agy_native_wire_probe/finish_guard.py tools/agy_native_wire_probe/symbols.py tools/agy_native_wire_probe/usage.py /app/tools/agy_native_wire_probe/

# Neutral cwd so no workspace rules leak into chat sessions
RUN mkdir -p /workspace-empty
ENV AGY_PROXY_CWD=/workspace-empty \
    AGY_PROXY_HOST=0.0.0.0 \
    AGY_PROXY_DATA_DIR=/root/.agy-proxy \
    AGY_PROXY_BIN=/opt/agy-bridge/agy \
    AGY_PROXY_BRIDGE=true

LABEL org.opencontainers.image.title="agy-proxy" \
      org.opencontainers.image.description="Gemini / OpenAI compatible reverse proxy backed by the Antigravity CLI (agy) native bridge" \
      org.opencontainers.image.version="0.0.17" \
      org.opencontainers.image.source="https://github.com/un4gt/agy-proxy"

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8317/health', timeout=3)"]

EXPOSE 8317
CMD ["python", "main.py"]
