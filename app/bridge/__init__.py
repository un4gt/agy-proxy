"""Fixed-SHA native bridge for the pinned Antigravity CLI.

The patcher and bridge client are intentionally isolated from the legacy
stream-json session manager.  They only support the SHA-pinned Linux 1.4.2
build; every other input is rejected before any bytes are written.
"""
from __future__ import annotations

__all__ = ["PATCHED_SHA256", "SOURCE_SHA256"]

SOURCE_SHA256 = "085f43e0d0628a8a8dabe1e547067818fa2c0adc86cfac98a3c4492918f455ef"
PATCHED_SHA256 = "636570d016f462b68382781917146ec8756dce416895bdfecfdd151f0a060649"
