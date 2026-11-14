"""Logging setup with an opt-in TRACE level.

The proxy's request logs intentionally avoid prompt bodies.  TRACE is for
local diagnosis: it may include structural metadata such as system-prompt
length and hashes, but not the prompt text unless ``AGY_PROXY_TRACE_PROMPTS``
is explicitly enabled.
"""
from __future__ import annotations

import logging
import os

TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def _trace(self: logging.Logger, message: str, *args, **kwargs) -> None:
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


if not hasattr(logging.Logger, "trace"):
    logging.Logger.trace = _trace  # type: ignore[attr-defined]


def configure_logging() -> None:
    name = (os.environ.get("AGY_PROXY_LOG_LEVEL") or "INFO").strip().upper()
    level = TRACE if name == "TRACE" else getattr(logging, name, logging.INFO)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("agy_proxy").setLevel(level)
