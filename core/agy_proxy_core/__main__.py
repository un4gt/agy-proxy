"""Core-only entry point: Gemini API + dashboard + account management."""

from __future__ import annotations

import uvicorn

from .app import create_core_only_app
from .config import load_config


def main() -> None:
    cfg = load_config()
    uvicorn.run(create_core_only_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
