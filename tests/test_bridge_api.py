"""Chat Completions routing through the bridge-only application."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.bridge.session import BridgeSessionManager
from app.config import Config


class FakeSession:
    account_id = "acct-1"
    account_name = "Test Account"
    turns = 0
    alive = False
    last_used = 0.0

    async def stream(self, payload):
        yield {"deltaText": "pong"}
        yield {"usage": {"inputTokens": "4", "outputTokens": "1"}}
        yield {"stopReason": "STOP", "messageId": "msg-1"}


def test_chat_completions_uses_the_bridge(monkeypatch, tmp_path):
    from app import agy, create_app, protocol_common

    async def models(_cfg, **_kwargs):
        return ["gemini-3.8-flash-medium"]

    async def acquire(self, **_kwargs):
        return FakeSession()

    monkeypatch.setattr(agy, "list_models", models)
    monkeypatch.setattr(protocol_common, "list_models", models)
    monkeypatch.setattr(BridgeSessionManager, "acquire", acquire)
    cfg = Config(data_dir=str(tmp_path), api_keys=("test",))
    with TestClient(create_app(cfg)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test"},
            json={"model": "gemini-3.8-flash-medium",
                  "messages": [{"role": "user", "content": "ping"}]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["choices"][0]["message"]["content"] == "pong"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["usage"]["prompt_tokens"] == 4
        health = client.get("/health").json()
        assert health["chat_backend"] == "bridge"
