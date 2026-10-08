"""Panel password auth + upstream API key auth.

Two distinct secrets, deliberately separate:
- `AGY_PROXY_PASSWORD` guards the admin panel (this server's own pages and
  management endpoints). It is never a credential for the API surface.
- Upstream API keys are created from the panel and are the *only* thing an
  API client presents. They can be revoked individually; the env var
  `AGY_PROXY_API_KEYS` still works as a fixed, non-revocable key set.
"""

from __future__ import annotations

import base64
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import Config

COOKIE_NAME = "agy_proxy_panel"
COOKIE_MAX_AGE = 7 * 24 * 3600


def extract_key(request: Request) -> str | None:
    """Pull a credential out of any header convention both API families use."""
    header = request.headers.get("x-goog-api-key")
    if header:
        return header.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth.lower().startswith("basic "):
        # Programmatic callers (curl -u :key): the key may be in either slot.
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", errors="replace")
        except Exception:
            return None
        user, _, password = decoded.partition(":")
        return password.strip() or user.strip()
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        return cookie.strip()
    header = request.headers.get("x-api-key")
    if header:
        return header.strip()
    # Gemini clients may also pass ?key=
    return request.query_params.get("key")


def _match(candidate: str | None, secret: str | None) -> bool:
    return bool(candidate) and bool(secret) and secrets.compare_digest(candidate, secret)


def panel_credential_valid(cfg: Config, candidate: str | None) -> bool:
    """Panel credentials: the PASSWORD, or an env-configured API key.

    Env keys are deployment-level secrets and have always unlocked the web UI,
    so they keep doing so. Keys created *in* the panel are upstream-only and
    deliberately cannot open it.
    """
    if _match(candidate, cfg.password):
        return True
    return any(_match(candidate, key) for key in cfg.api_keys)


def panel_authorized(cfg: Config, request: Request) -> bool:
    """Whether the panel pages/management endpoints may be used."""
    if not cfg.password and not cfg.api_keys:
        return True
    return panel_credential_valid(cfg, extract_key(request))


def api_key_record(cfg: Config, request: Request):
    """The user-created key record behind this request, or None for env keys."""
    store = getattr(request.app.state, "store", None)
    if store is None:
        return None
    return store.find_key(extract_key(request))


def is_authorized(cfg: Config, request: Request) -> bool:
    """Auth for the API surface: a panel credential or a panel-created key."""
    if not cfg.auth_enabled:
        return True
    candidate = extract_key(request)
    if panel_credential_valid(cfg, candidate):
        return True
    return api_key_record(cfg, request) is not None


# -- /signin: single-field panel login (sets an HttpOnly cookie) -------------

signin_router = APIRouter()

@signin_router.get("/signin")
async def signin_page(request: Request):
    from .ui import ui_page
    return ui_page()


@signin_router.post("/signin")
async def signin_submit(request: Request):
    cfg = request.app.state.cfg
    try:
        body = await request.json()
        key = str((body or {}).get("key", "")).strip()
    except Exception:
        key = ""
    if not panel_credential_valid(cfg, key):
        return JSONResponse(status_code=401, content={"error": "PASSWORD 不正确"})
    response = JSONResponse({"ok": True})
    response.set_cookie(
        COOKIE_NAME, key, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/"
    )
    return response


@signin_router.get("/signout")
async def signout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


def gemini_error(status_code: int, message: str, grpc_status: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": status_code, "message": message, "status": grpc_status}},
    )


def openai_error(status_code: int, message: str, err_type: str, code: str | None = None,
                 *, param: str | None = None) -> JSONResponse:
    body: dict = {"error": {"message": message, "type": err_type, "param": param, "code": code}}
    return JSONResponse(status_code=status_code, content=body)
