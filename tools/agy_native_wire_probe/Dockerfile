FROM tumuer/agy-proxy:v0.0.17-native
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends gdb binutils tcpdump \
    && rm -rf /var/lib/apt/lists/*
COPY app /app/app
COPY tools/agy_native_wire_probe /app/tools/agy_native_wire_probe
ENV AGY_WIRE_BINARY=/opt/agy-native/agy
ENTRYPOINT []
CMD ["sleep", "infinity"]
