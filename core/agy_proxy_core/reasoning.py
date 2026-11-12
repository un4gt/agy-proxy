"""Resolve OpenAI reasoning effort against the CLI's advertised model variants."""

from __future__ import annotations

from .agy import AgyError

CLI_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def model_family(model: str) -> str:
    """Return the base ID shared by a model and its reasoning variants."""
    base, _, suffix = model.rpartition("-")
    return base if base and suffix in CLI_EFFORTS else model


class ReasoningError(AgyError):
    def __init__(self, message: str):
        super().__init__(message, status_code=400)


def parse_effort(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReasoningError("reasoning_effort must be a string or null")
    level = value.strip().lower()
    if level == "minimal":
        # Preserve the existing proxy alias for the lowest CLI effort.
        return "low"
    if level == "none":
        raise ReasoningError("reasoning_effort 'none' is not supported by agy; reasoning cannot be disabled")
    if level not in CLI_EFFORTS:
        raise ReasoningError("reasoning_effort must be one of: minimal, low, medium, high, xhigh, max")
    return level


def model_families(slugs: list[str]) -> dict[str, dict[str, str]]:
    families: dict[str, dict[str, str]] = {}
    for slug in slugs:
        base, _, suffix = slug.rpartition("-")
        if base and suffix in CLI_EFFORTS:
            families.setdefault(base, {})[suffix] = slug
    return families


def apply_effort(model: str | None, effort: str | None, slugs: list[str]) -> tuple[str | None, str | None]:
    if effort is None or model is None or model == "auto":
        return model, effort
    families = model_families(slugs)
    base, _, suffix = model.rpartition("-")
    family = base if suffix in CLI_EFFORTS and base in families else model
    variants = families.get(family)
    if variants is None:
        # Explicit custom MODEL_MAP targets without catalog variants still use
        # agy's documented --effort flag; do not manufacture a model ID.
        return model, effort
    if effort not in variants:
        supported = ", ".join(level for level in CLI_EFFORTS if level in variants)
        raise ReasoningError(
            f"Model '{family}' does not support reasoning_effort '{effort}'. Supported values: {supported}"
        )
    return variants[effort], effort
