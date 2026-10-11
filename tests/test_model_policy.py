"""Model-family switches persist and apply to discovery and inference APIs."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import agy, create_app, dashboard, openai_api
from app.config import Config
from app.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    fake = (Path(__file__).parent / "fake_tools_agy.py").as_posix()
    cfg = Config(agy_bin=f"{sys.executable} {fake}", data_dir=str(tmp_path),
                 default_model="gemini-3-pro-high",
                 model_map={"alias": "gemini-3-pro-low"})
    monkeypatch.setenv("FAKE_AGY_ARGV_FILE", str(tmp_path / "argv.jsonl"))
    agy._model_caches.clear()
    with TestClient(create_app(cfg)) as c:
        yield c


def disable(client, model="gemini-3-pro"):
    response = client.patch(f"/api/models/{model}", json={"enabled": False})
    assert response.status_code == 200, response.text


def test_family_switch_discovery_persistence_and_restore(client):
    original = client.get("/v1/models").json()["data"]
    disable(client)
    public = client.get("/v1/models").json()["data"]
    assert public == [m for m in original if not m["id"].startswith("gemini-3-pro")]
    assert public  # Other families remain available.
    assert all("gemini-3-pro" not in m["name"]
               for m in client.get("/v1beta/models").json()["models"])
    admin = client.get("/api/models").json()["models"]
    family = [m for m in admin if m["family"] == "gemini-3-pro"]
    assert family and all(not m["enabled"] for m in family)
    for model in ("gemini-3-pro", "gemini-3-pro-low", "gemini-3-pro-high", "alias"):
        assert client.get(f"/v1/models/{model}").status_code == 404
    restored = Store(client.app.state.cfg)
    restored.load()
    assert not restored.model_enabled("gemini-3-pro-high")
    assert restored.model_enabled("gemini-3-pro-preview-high")
    assert restored.model_enabled("gemini-3-flash-low")
    # A variant switch updates the same family and restores all variants.
    assert client.patch("/api/models/gemini-3-pro-low", json={"enabled": True}).status_code == 200
    assert client.get("/v1/models").json()["data"] == original
    assert client.get("/v1/models/gemini-3-pro-high").status_code == 200


@pytest.mark.parametrize("model", ["gemini-3-pro", "gemini-3-pro-low", "gemini-3-pro-high", "alias", "default", ""])
@pytest.mark.parametrize("stream", [False, True])
def test_disabled_chat_models_aliases_and_defaults_are_rejected(client, model, stream):
    disable(client)
    response = client.post("/v1/chat/completions", json={
        "model": model, "stream": stream, "reasoning_effort": "high",
        "messages": [{"role": "user", "content": "PING"}],
    })
    assert response.status_code == 404, response.text
    assert not (Path(client.app.state.cfg.data_dir) / "argv.jsonl").exists()


@pytest.mark.parametrize("path,body", [
    ("/v1/responses", {"input": "PING"}),
    ("/v1/messages", {"max_tokens": 20, "messages": [{"role": "user", "content": "PING"}]}),
    ("/v1beta/models/gemini-3-pro-high:generateContent", {"contents": [{"parts": [{"text": "PING"}]}]}),
    ("/v1beta/models/gemini-3-pro-low:streamGenerateContent", {"contents": [{"parts": [{"text": "PING"}]}]}),
])
def test_other_protocols_reject_disabled_models(client, path, body):
    disable(client)
    response = client.post(path, json={"model": "gemini-3-pro-high", **body})
    assert response.status_code == 404, response.text
    assert not (Path(client.app.state.cfg.data_dir) / "argv.jsonl").exists()


def test_catalog_outage_does_not_bypass_disable(client, monkeypatch):
    disable(client)

    async def unavailable(_cfg, **_kwargs):
        raise agy.AgyError("catalog unavailable")

    monkeypatch.setattr(openai_api, "list_models", unavailable)
    response = client.post("/v1/chat/completions", json={
        "model": "alias", "messages": [{"role": "user", "content": "PING"}],
    })
    assert response.status_code == 404
    monkeypatch.setattr(dashboard, "list_models", unavailable)
    assert client.patch("/api/models/gemini-3-pro", json={"enabled": True}).status_code == 200


@pytest.mark.parametrize("body", [{}, {"enabled": "false"}, {"enabled": 0}, [], None])
def test_switch_requires_boolean(client, body):
    assert client.patch("/api/models/gemini-3-pro", json=body).status_code == 400
    assert not client.app.state.store.disabled_models


def test_switch_requires_panel_credentials(client):
    key = client.post("/api/keys", json={"name": "client"}).json()["key"]
    client.app.state.cfg.password = "panel-secret"
    for headers in ({}, {"Authorization": f"Bearer {key}"}):
        assert client.patch("/api/models/gemini-3-pro", json={"enabled": False}, headers=headers).status_code == 401
    assert client.patch("/api/models/gemini-3-pro", json={"enabled": False},
                        headers={"Authorization": "Bearer panel-secret"}).status_code == 200


def test_unknown_family_cannot_be_disabled(client):
    assert client.patch("/api/models/missing", json={"enabled": False}).status_code == 404


def test_new_variants_inherit_disable_without_affecting_similar_names(client, monkeypatch):
    slugs = ["gemini-3.8-flash-low", "gemini-3.8-flash-high", "gemini-3.8-flash-lite-low"]

    async def catalog(_cfg, **_kwargs):
        return slugs

    monkeypatch.setattr(dashboard, "list_models", catalog)
    monkeypatch.setattr(openai_api, "list_models", catalog)
    disable(client, "gemini-3.8-flash")
    slugs.append("gemini-3.8-flash-max")
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert ids == ["gemini-3.8-flash-lite-low", "gemini-3.8-flash-lite"]
    assert client.get("/v1/models/gemini-3.8-flash-max").status_code == 404


def test_save_failure_does_not_report_success_or_change_policy(client, monkeypatch):
    monkeypatch.setattr(client.app.state.store, "_write", lambda: None)
    assert client.patch("/api/models/gemini-3-pro", json={"enabled": False}).status_code == 500
    assert client.app.state.store.model_enabled("gemini-3-pro-high")
