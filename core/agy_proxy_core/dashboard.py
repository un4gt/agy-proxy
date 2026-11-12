"""Dashboard data + API key management routes.

Panel-only: all of these are gated by the panel password (not by an upstream
API key), because they manage the data that API keys themselves are checked
against.
"""

from __future__ import annotations

from . import json_codec as json

import logging

from fastapi import APIRouter, Request
from .json_codec import JSONResponse

from .agy import AgyError, list_models, model_labels
from .config import Config
from .reasoning import model_family
from .store import Store

log = logging.getLogger("agy_proxy.dashboard")

router = APIRouter()


def _panel_denied(request: Request) -> JSONResponse | None:
    from .auth import panel_authorized

    cfg: Config = request.app.state.cfg
    if panel_authorized(cfg, request):
        return None
    return JSONResponse(status_code=401, content={"error": {"message": "面板密码无效或缺失"}})


def _store(request: Request) -> Store:
    store = getattr(request.app.state, "store", None)
    if store is None:
        store = Store(request.app.state.cfg)
        request.app.state.store = store
    return store


@router.get("/dashboard")
async def dashboard(request: Request) -> JSONResponse:
    """Totals, per-model usage, recent turns and key metadata (no secrets)."""
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    store = _store(request)
    snapshot = store.snapshot()
    labels = model_labels(request.app.state.cfg)
    for row in snapshot["models"]:
        row["label"] = labels.get(row["model"], "")
    return JSONResponse(snapshot)


@router.get("/api/models")
async def api_models(request: Request) -> JSONResponse:
    """Model slugs agy currently offers, with human labels when available."""
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    cfg: Config = request.app.state.cfg
    try:
        slugs = await list_models(cfg)
    except AgyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    labels = model_labels(cfg)
    store = _store(request)
    return JSONResponse({"models": [
        {"id": s, "label": labels.get(s, ""), "family": model_family(s),
         "enabled": store.model_enabled(s)} for s in slugs
    ]})


@router.patch("/api/models/{model_id}")
async def set_model_enabled(model_id: str, request: Request) -> JSONResponse:
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    try:
        body = json.loads(await request.body())
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return JSONResponse({"error": {"message": "enabled 必须为布尔值"}}, status_code=400)
    store = _store(request)
    family = model_family(model_id)
    # Previously disabled families can be restored even during a CLI outage.
    if family not in store.disabled_models:
        try:
            slugs = await list_models(request.app.state.cfg)
        except AgyError as exc:
            return JSONResponse({"error": {"message": str(exc)}}, status_code=exc.status_code)
        if family not in {model_family(slug) for slug in slugs}:
            return JSONResponse({"error": {"message": "没有这个模型"}}, status_code=404)
    try:
        await store.set_model_enabled(family, body["enabled"])
    except OSError as exc:
        return JSONResponse({"error": {"message": str(exc)}}, status_code=500)
    return JSONResponse({"family": family, "enabled": store.model_enabled(family)})


@router.post("/api/keys")
async def create_key(request: Request) -> JSONResponse:
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    try:
        body = json.loads(await request.body())
    except Exception:
        body = {}
    name = str((body or {}).get("name") or "")
    record = await _store(request).create_key(name)
    # The plaintext key is returned exactly once, at creation time.
    return JSONResponse(record.public() | {"key": record.key}, status_code=201)


@router.patch("/api/keys/{key_id}")
async def rename_key(key_id: str, request: Request) -> JSONResponse:
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    try:
        body = json.loads(await request.body())
    except Exception:
        body = {}
    record = await _store(request).rename_key(key_id, str((body or {}).get("name") or ""))
    if record is None:
        return JSONResponse({"error": {"message": "没有这把 API Key"}}, status_code=404)
    return JSONResponse(record.public())


@router.delete("/api/keys/{key_id}")
async def delete_key(key_id: str, request: Request) -> JSONResponse:
    denied = _panel_denied(request)
    if denied is not None:
        return denied
    if not await _store(request).delete_key(key_id):
        return JSONResponse({"error": {"message": "没有这把 API Key"}}, status_code=404)
    return JSONResponse({"ok": True})
