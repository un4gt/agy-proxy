"""Apply persisted model-family switches at public API boundaries."""

from dataclasses import replace

from fastapi import Request

from .agy import AgyError, resolve_model
from .config import Config


def enabled_models(request: Request, slugs: list[str], *, owner: str | None = None) -> list[str]:
    store = request.app.state.store
    allowed = [slug for slug in slugs if store.model_enabled(slug)]
    if owner is None:
        return allowed
    pool = getattr(request.app.state.cfg, "account_pool", None)
    if pool is None or not pool.enabled:
        return allowed
    return [slug for slug in allowed if pool.model_available_for_owner(owner, slug)]


def require_enabled_model(request: Request, *models: str | None) -> None:
    store = request.app.state.store
    for model in models:
        if not store.model_enabled(model):
            raise AgyError(f"Model '{model}' is disabled", status_code=404)


def resolve_requested_model(requested: str, cfg: Config, slugs: list[str]) -> str | None:
    """Map an explicit client model onto an agy slug, or raise 404."""
    if requested in cfg.model_map:
        return cfg.model_map[requested]
    if not requested or requested.lower() in ("default", "(default)"):
        return cfg.default_model
    if requested.lower() == "auto":
        return "auto"
    resolved = resolve_model(requested, replace(cfg, default_model=None), slugs)
    if slugs and resolved not in slugs:
        raise AgyError(f"Model '{requested}' not found", status_code=404, code="model_not_found")
    return resolved
