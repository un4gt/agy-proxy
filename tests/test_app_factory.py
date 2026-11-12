"""Core-only and full application factories stay separated."""

from __future__ import annotations

from fastapi.testclient import TestClient

import agy_proxy_adapters.openai_api as openai_api
import agy_proxy_core.agy as core_agy
import agy_proxy_core.gemini_api as gemini_api
from agy_proxy_adapters import create_full_app, install
from agy_proxy_core.app import create_core_only_app
from agy_proxy_core.config import Config


async def _models(*args, **kwargs):
    return ["gemini-3.8-flash-medium"]


def _patched(monkeypatch):
    monkeypatch.setattr(core_agy, "list_models", _models)
    monkeypatch.setattr(gemini_api, "list_models", _models)
    monkeypatch.setattr(openai_api, "list_models", _models)


def test_core_only_has_gemini_but_not_chat(monkeypatch, tmp_path):
    _patched(monkeypatch)
    app = create_core_only_app(Config(data_dir=str(tmp_path), api_keys=("test",)))
    headers = {"Authorization": "Bearer test"}
    with TestClient(app) as client:
        assert client.post("/v1/chat/completions", headers=headers, json={}).status_code == 404
        response = client.get("/v1/models", headers=headers)
        assert response.status_code == 200
        assert "models" in response.json()
        response = client.post(
            "/v1beta/models/gemini-3.8-flash-medium:generateContent",
            headers=headers,
            json={},
        )
        assert response.status_code == 400
        assert "error" in response.json()
        about = client.get("/about").json()
        assert about["service"] == "agy-proxy-core"
        assert about["edition"] == "core"
        assert client.get("/health").json()["edition"] == "core"


def test_full_has_chat_and_model_shape_dispatch(monkeypatch, tmp_path):
    _patched(monkeypatch)
    app = create_full_app(Config(data_dir=str(tmp_path), api_keys=("test",)))
    headers = {"Authorization": "Bearer test"}
    with TestClient(app) as client:
        assert client.post("/v1/chat/completions", headers=headers, json={}).status_code == 400
        response = client.get("/v1/models", headers=headers)
        assert response.status_code == 200
        assert "data" in response.json()
        response = client.get(
            "/v1/models",
            headers={**headers, "x-goog-api-client": "google-genai-sdk/3.1.0"},
        )
        assert response.status_code == 200
        assert "models" in response.json()
        about = client.get("/about").json()
        assert about["service"] == "agy-proxy-full"
        assert about["edition"] == "full"
        assert client.get("/health").json()["edition"] == "full"


def test_install_is_idempotent(monkeypatch, tmp_path):
    _patched(monkeypatch)
    app = create_full_app(Config(data_dir=str(tmp_path), api_keys=("test",)))
    before = len(app.routes)
    install(app, app.state.services)
    install(app, app.state.services)
    assert len(app.routes) == before
