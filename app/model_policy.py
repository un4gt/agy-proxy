"""Apply persisted model-family switches at public API boundaries."""

from fastapi import Request

from .agy import AgyError


def enabled_models(request: Request, slugs: list[str]) -> list[str]:
    store = request.app.state.store
    return [slug for slug in slugs if store.model_enabled(slug)]


def require_enabled_model(request: Request, *models: str | None) -> None:
    store = request.app.state.store
    for model in models:
        if not store.model_enabled(model):
            raise AgyError(f"Model '{model}' is disabled", status_code=404)
