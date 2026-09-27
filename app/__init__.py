"""agy-proxy FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import dashboard, gemini_api, login, openai_api, update
from .agy import SessionManager
from .auth import panel_authorized, signin_router
from .config import Config, load_config
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agy_proxy")


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def sweeper() -> None:
            interval = min(60.0, cfg.session_ttl / 2)
            # Warm one agy process per configured prewarm model right away so
            # the first chat request skips the process cold start.
            try:
                await app.state.sessions.ensure_prewarm()
            except Exception:  # pragma: no cover - keep the sweeper alive
                log.exception("session prewarm failed")
            while True:
                await asyncio.sleep(interval)
                try:
                    await app.state.sessions.sweep()
                    await app.state.sessions.ensure_prewarm()
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
            "agy-proxy ready (bin=%s max_sessions=%d turn_timeout=%.0fs panel_auth=%s upstream_keys=%d)",
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
            updater = getattr(app.state, "update_bridge", None)
            if updater is not None:
                await updater.shutdown()
            await app.state.store.flush()
            await app.state.sessions.close_all()

    app = FastAPI(
        title="agy-proxy",
        version="0.0.11",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.cfg = cfg
    app.state.sessions = SessionManager(cfg)
    app.state.store = Store(cfg)
    app.state.store.load()

    # OpenAI router first: its exact /v1/chat/completions route must win over
    # the Gemini catch-all POST /v1/{rest:path}.
    app.include_router(openai_api.router)
    app.include_router(gemini_api.router)
    app.include_router(login.router)
    app.include_router(update.router)
    app.include_router(dashboard.router)
    app.include_router(signin_router)

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        # Unauthorized browsers are sent to the panel sign-in page.
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

    # Optional vendored bundles (marked / DOMPurify) kept reachable for anyone
    # still pointing a client at them; the dashboard itself needs no externals.
    static_dir = Path(__file__).parent / "static"

    # Stable bundle names are revalidated, so rebuilt UIs appear on refresh.
    class FrontendAssets(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/assets", FrontendAssets(directory=static_dir / "assets"), name="ui-assets")

    @app.get("/app.css", include_in_schema=False)
    async def app_css():
        # Deliberately NOT `no-store` like the pages above: this is a stylesheet,
        # not a document. `no-cache` means "store it, but revalidate before every
        # use" — the filename carries no content hash, so a long max-age would
        # strand returned visitors on an old rule set for a day.
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
        return JSONResponse(
            {
                "service": "agy-proxy",
                "version": "0.0.11",
                "endpoints": [
                    "/ (dashboard)",
                    "/login (agy OAuth)",
                    "/v1/chat/completions",
                    "/v1/models",
                    "/v1beta/models/{model}:generateContent",
                    "/v1beta/models/{model}:streamGenerateContent",
                ],
            }
        )

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        # Deliberately minimal: no binary paths, no session details.
        return JSONResponse(
            {
                "status": "ok",
                "version": "0.0.11",
                "auth_required": cfg.auth_enabled,
                "signed_in": (Path.home() / ".gemini" / "oauth_creds.json").exists(),
            }
        )

    return app


app = create_app()
