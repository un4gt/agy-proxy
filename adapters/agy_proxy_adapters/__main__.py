"""Full-image entry point: core application plus protocol adapters."""

from __future__ import annotations

import uvicorn

from agy_proxy_core.config import load_config

from . import create_full_app


def main() -> None:
    cfg = load_config()
    uvicorn.run(create_full_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
