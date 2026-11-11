"""Model identifiers advertised by the pinned 1.4.2 catalog.

The CLI resolves a slug to an internal enum before calling GetChatMessage.
The bridge must do the same; the enum is not a stable public API.
"""
from __future__ import annotations

# Slugs printed by `agy models` for the pinned build, mapped to the
# codeium_common_pb.Model enum values observed in its catalog.
MODEL_IDS = {
    "chat_20706": 235,
    "chat_23310": 269,
    "claude-opus-5-5-low": 1400,
    "claude-opus-5-5-medium": 1401,
    "claude-opus-5-5-high": 1402,
    "claude-sonnet-5-5-low": 1403,
    "claude-sonnet-5-5-medium": 1404,
    "claude-sonnet-5-5-high": 1405,
    "gemini-2.5-flash": 312,
    "gemini-2.5-flash-lite": 330,
    "gemini-2.5-flash-thinking": 313,
    "gemini-2.5-pro": 246,
    "gemini-3-flash": 1018,
    "gemini-3.1-flash-image": 1021,
    "gemini-3.1-flash-lite": 1050,
    "gemini-3.5-flash-lite": 1198,
    "gemini-3.8-flash-high": 1318,
    "gemini-3.8-flash-medium": 1319,
    "gemini-3.8-flash-low": 1320,
    "gemini-3.8-flash-tiered": 1322,
    "gemini-4-argon-high": 1417,
    "gemini-4-argon-medium": 1418,
    "gemini-4-argon-low": 1419,
    "gemini-4-carbon-disabled": 1670,
    "gpt-oss-120b-medium": 342,
    "tab_flash_lite_preview": 1019,
    "tab_jump_flash_lite_preview": 1028,
    # Base names have no catalog variant of their own; use the medium variant
    # so requests without an explicit effort still select a concrete model.
    "gemini-3.8-flash": 1319,
    "gemini-4-argon": 1418,
    "claude-opus-5-5": 1401,
    "claude-sonnet-5-5": 1404,
    "gpt-oss-120b": 342,
}

# The CLI's own first agent turn uses this catalog default.
DEFAULT_MODEL_ID = MODEL_IDS["gemini-4-argon-medium"]

# CompletionConfiguration.thinking_level values observed in the catalog.
EFFORT_LEVELS = {"low": 1, "medium": 2, "high": 3}


def model_id(slug: str | None) -> int:
    if not slug or slug == "auto":
        return DEFAULT_MODEL_ID
    try:
        return MODEL_IDS[slug]
    except KeyError:
        raise ValueError(f"no bridge model mapping for {slug!r}") from None


def has_model(slug: str) -> bool:
    return slug in MODEL_IDS
