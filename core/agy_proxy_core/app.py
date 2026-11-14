"""FastAPI application factory for agy-proxy-core."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import account, dashboard, gemini_api, login, update
from .account_pool import AccountPool
from .api import CoreServices
from .auth import panel_authorized, signin_router
from .bridge.session import BridgeSessionManager
from .config import Config, load_config
from .json_codec import JSONResponse
from .logging_setup import configure_logging
from .store import Store

configure_logging()
log = logging.getLogger("agy_proxy")


def create_app(cfg: Config | None = None, *, core_only: bool = False) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def sweeper() -> None:
            interval = min(60.0, cfg.session_ttl / 2)
            while True:
                await asyncio.sleep(interval)
                try:
                    await app.state.sessions.sweep()
                except Exception:  # pragma: no cover - keep the sweeper alive
                    log.exception("session sweep failed")

        task = asyncio.create_task(sweeper(), name="agy-session-sweeper")
        from .agy import list_models

        prewarm = asyncio.create_task(list_models(cfg), name="agy-models-prewarm")
        if not cfg.password:
            log.warning(
                "AGY_PROXY_PASSWORD is NOT set — anyone who can reach this port can open the "
                "panel, create API keys and trigger the agy login flow. Set it for any non-local deployment."
            )
        elif cfg.password_from_api_keys:
            log.warning(
                "AGY_PROXY_PASSWORD is not set; the first AGY_PROXY_API_KEYS entry is being used "
                "as the panel password. Set AGY_PROXY_PASSWORD explicitly."
            )
        log.info(
            "agy-proxy core ready (bin=%s max_sessions=%d turn_timeout=%.0fs panel_auth=%s upstream_keys=%d)",
            cfg.agy_bin, cfg.max_sessions, cfg.turn_timeout, bool(cfg.password), len(cfg.api_keys),
        )
        try:
            yield
        finally:
            task.cancel()
            prewarm.cancel()
            bridge = getattr(app.state, "login_bridge", None)
            if bridge is not None:
                bridge.shutdown()
            for bridge in getattr(app.state, "account_login_bridges", {}).values():
                bridge.shutdown()
            updater = getattr(app.state, "update_bridge", None)
            if updater is not None:
                await updater.shutdown()
            await app.state.store.flush()
            await app.state.sessions.close_all()

    app = FastAPI(
        title="agy-proxy-core",
        default_response_class=JSONResponse,
        version="0.0.20",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.cfg = cfg
    app.state.edition = "core"
    from .request_context import RequestContextMiddleware

    app.add_middleware(RequestContextMiddleware)
    cfg.account_pool = AccountPool(cfg)
    app.state.account_pool = cfg.account_pool
    app.state.sessions = BridgeSessionManager(cfg)
    app.state.bridge_sessions = app.state.sessions
    app.state.store = Store(cfg)
    app.state.store.load()
    app.state.services = CoreServices(
        cfg=cfg,
        store=app.state.store,
        account_pool=cfg.account_pool,
        bridge_sessions=app.state.sessions,
        gemini_service=None,
    )

    app.include_router(gemini_api.router)
    app.include_router(login.router)
    app.include_router(update.router)
    app.include_router(dashboard.router)
    app.include_router(account.router)
    app.include_router(signin_router)

    if core_only:
        @app.get("/v1/models", include_in_schema=False)
        async def core_models(request: Request) -> Response:
            return await gemini_api.gemini_models(request)

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        if not panel_authorized(cfg, request):
            return RedirectResponse("/signin", status_code=307)
        from .ui import ui_page

        return ui_page()

    _FAVICON = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
        "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0' stop-color='#262626'/><stop offset='1' stop-color='#171717'/>"
        "</linearGradient></defs>"
        "<rect width='64' height='64' rx='14' fill='url(#g)'/>"
        "<path d='M32 14 L47 50 H40.5 L37 41 H27 L23.5 50 H17 Z M32 25.5 L28.8 35 H35.2 Z' fill='#ffffff'/>"
        "</svg>"
    )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(
            content=_FAVICON, media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    static_dir = Path(__file__).parent / "static"

    class FrontendAssets(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/assets", FrontendAssets(directory=static_dir / "assets"), name="ui-assets")

    @app.get("/app.css", include_in_schema=False)
    async def app_css():
        return FileResponse(
            static_dir / "app.css",
            media_type="text/css",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/marked.min.js", include_in_schema=False)
    async def marked_js():
        return FileResponse(
            static_dir / "marked.min.js",
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/purify.min.js", include_in_schema=False)
    async def purify_js():
        return FileResponse(
            static_dir / "purify.min.js",
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/about")
    async def about() -> JSONResponse:
        endpoints = [
            "/ (dashboard)",
            "/login (agy OAuth)",
            "/v1beta/models/{model}:generateContent",
            "/v1beta/models/{model}:streamGenerateContent",
            "/v1beta/models/{model}:countTokens",
            "/v1beta/models",
        ]
        if core_only:
            endpoints.append("/v1/models (Gemini alias)")
        edition = getattr(app.state, "edition", "core")
        return JSONResponse(
            {
                "service": f"agy-proxy-{edition}",
                "edition": edition,
                "version": "0.0.20",
                "endpoints": endpoints,
            }
        )

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "edition": getattr(app.state, "edition", "core"),
                "version": "0.0.20",
                "auth_required": cfg.auth_enabled,
                "chat_backend": "bridge",
                "signed_in": (any(p.credentials.is_file() for p in cfg.account_pool.profiles())
                              if cfg.account_pool.enabled else account.credentials_present()),
            }
        )

    return app


def create_core_only_app(cfg: Config | None = None) -> FastAPI:
    return create_app(cfg, core_only=True)
