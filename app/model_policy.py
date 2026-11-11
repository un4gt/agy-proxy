"""Apply persisted model-family switches at public API boundaries."""

from fastapi import Request

from .agy import AgyError


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
