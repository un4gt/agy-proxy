"""agy-proxy protocol adapters package.

The adapters expose OpenAI/Responses/Anthropic wire protocols and depend only
on ``agy_proxy_core``. The core application never imports this package.
"""

from agy_proxy_core.api import CoreServices

__version__ = "0.0.20"


class GeminiModelsAliasMiddleware:
    """Route Gemini SDK model-list calls to the core Gemini shape."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and scope.get("path") == "/v1/models"
        ):
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            client = headers.get(b"x-goog-api-client", b"").lower()
            agent = headers.get(b"user-agent", b"").lower()
            if b"google-genai-sdk" in client or b"google-genai-sdk" in agent:
                scope = dict(scope, path="/v1beta/models", raw_path=b"/v1beta/models")
        await self.app(scope, receive, send)


def install(app, services: CoreServices | None = None) -> None:
    """Install the three downstream protocol routers on a core app."""
    app.state.edition = "full"
    if getattr(app.state, "adapters_installed", False):
        return
    from . import anthropic_api, openai_api, responses_api

    app.include_router(openai_api.router)
    app.include_router(responses_api.router)
    app.include_router(anthropic_api.router)
    app.add_middleware(GeminiModelsAliasMiddleware)
    app.state.adapters_installed = True


def create_full_app(cfg=None):
    from agy_proxy_core.app import create_app as create_core_app

    app = create_core_app(cfg)
    install(app, app.state.services)
    return app


__all__ = ["__version__", "create_full_app", "install"]
