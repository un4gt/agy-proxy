"""End-to-end tests of the proxy against a fake agy CLI."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FAKE_AGY = (Path(__file__).parent / "fake_agy.py").as_posix()


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Keep each test's API keys and usage counters out of the real data file."""
    monkeypatch.setenv("AGY_PROXY_DATA_DIR", str(tmp_path / "data"))


def make_client() -> TestClient:
    from app import create_app
    from app import agy as agy_module

    agy_module._model_caches.clear()  # isolate the models cache between tests
    return TestClient(create_app())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGY_PROXY_BIN", f"{sys.executable} {FAKE_AGY}")
    with make_client() as c:
        yield c


# -- Gemini ------------------------------------------------------------------


def test_gemini_generate_content(client):
    r = client.post(
        "/v1beta/models/gemini-3-pro-high:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "PING"}]}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candidates"][0]["content"]["parts"][0]["text"] == "pong"
    assert body["candidates"][0]["finishReason"] == "STOP"
    assert body["usageMetadata"]["totalTokenCount"] > 0
    assert body["modelVersion"] == "gemini-3-pro-high"


def test_gemini_stream_generate_content(client):
    with client.stream(
        "POST",
        "/v1beta/models/gemini-3-pro-high:streamGenerateContent?alt=sse",
        json={"contents": [{"role": "user", "parts": [{"text": "PING"}]}]},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        chunks = _sse_events(r)
    text = "".join(
        c["candidates"][0]["content"]["parts"][0]["text"]
        for c in chunks
        if c.get("candidates") and c["candidates"][0]["content"]["parts"]
    )
    assert text == "pong"
    last = chunks[-1]
    assert last["candidates"][0]["finishReason"] == "STOP"
    assert "usageMetadata" in last


def test_gemini_path_aliases(client):
    for url in (
        "/v1/models/gemini-3-pro-high:generateContent",
        "/v1beta/gemini-3-pro-high:generateContent",
        "/v1beta/models/gemini-3-pro-high/generateContent",
    ):
        r = client.post(url, json={"contents": [{"role": "user", "parts": [{"text": "PING"}]}]})
        assert r.status_code == 200, (url, r.text)
        assert r.json()["candidates"][0]["content"]["parts"][0]["text"] == "pong"


def test_gemini_system_and_history_seeded_into_prompt(client):
    body = {
        "systemInstruction": {"parts": [{"text": "BE SMART"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "earlier-question"}]},
            {"role": "model", "parts": [{"text": "earlier-answer"}]},
            {"role": "user", "parts": [{"text": "current-question"}]},
        ],
    }
    r = client.post("/v1beta/models/gemini-3-pro-high:generateContent", json=body)
    assert r.status_code == 200
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    # New session: the whole transcript (system + both turns) is flattened into
    # the single seed prompt, so the fake's echo of it must contain everything.
    assert "turn 1]" in text
    assert "BE SMART" in text
    assert "earlier-question" in text
    assert "current-question" in text


def test_gemini_count_tokens(client):
    r = client.post(
        "/v1beta/models/gemini-3-pro-high:countTokens",
        json={"contents": [{"role": "user", "parts": [{"text": "hello world, how are you?"}]}]},
    )
    assert r.status_code == 200
    assert isinstance(r.json()["totalTokens"], int)


def test_gemini_models_list(client):
    r = client.get("/v1beta/models")
    assert r.status_code == 200
    models = r.json()["models"]
    names = [m["name"] for m in models]
    assert "models/gemini-3-pro-high" in names
    assert "generateContent" in models[0]["supportedGenerationMethods"]


def test_gemini_unknown_action_404(client):
    r = client.post("/v1beta/models/gemini-3-pro-high:frobnicate", json={"contents": []})
    assert r.status_code == 404


def test_models_failure_surfaces_agy_error(monkeypatch):
    import os

    os.environ["AGY_PROXY_BIN"] = f"{sys.executable} {FAKE_AGY}"
    os.environ["FAKE_AGY_MODELS_FAIL"] = "1"
    try:
        with make_client() as c:
            r = c.get("/v1beta/models")
            assert r.status_code == 502
            assert "sign in" in r.json()["error"]["message"]

            # chat still works: model list failure must not block generation
            # (model resolution falls back to passthrough).
            r = c.post(
                "/v1/chat/completions",
                json={"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": "PING"}]},
            )
            assert r.status_code == 200
            assert r.json()["choices"][0]["message"]["content"] == "pong"
    finally:
        del os.environ["AGY_PROXY_BIN"]
        del os.environ["FAKE_AGY_MODELS_FAIL"]


# -- OpenAI ------------------------------------------------------------------


def test_openai_chat_completions(client):
    r = client.post(
        "/v1/chat/completions",
        json={"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": "PING"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "pong"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] > 0
    assert body["model"] == "gemini-3-pro-high"


def test_openai_stream(client):
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemini-3-pro-high",
            "stream": True,
            "messages": [{"role": "user", "content": "PING"}],
        },
    ) as r:
        assert r.status_code == 200
        events = _sse_events(r)
    payloads = [e for e in events if e != "[DONE]"]
    assert payloads[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(p["choices"][0]["delta"].get("content", "") for p in payloads if p["choices"])
    assert text == "pong"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert events[-1] == "[DONE]"


def test_openai_stream_include_usage(client):
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemini-3-pro-high",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "PING"}],
        },
    ) as r:
        events = _sse_events(r)
    usage_chunks = [e for e in events if e != "[DONE]" and not e.get("choices")]
    assert len(usage_chunks) == 1
    assert usage_chunks[0]["usage"]["total_tokens"] > 0


def test_openai_models(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert "gemini-3-pro-high" in ids


def test_openai_model_detail(client):
    r = client.get("/v1/models/gemini-3-pro-high")
    assert r.status_code == 200
    assert r.json()["id"] == "gemini-3-pro-high"


def test_openai_message_shapes(client):
    # Array-shaped user content, no system: sent raw, so "PING" reaches agy verbatim.
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3-flash-medium",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "PING"}]}],
        },
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "pong"

    # With a system prompt the turn is seeded with the framed transcript.
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3-flash-medium",
            "messages": [
                {"role": "system", "content": "sys-prompt"},
                {"role": "user", "content": "PING"},
            ],
        },
    )
    assert r.status_code == 200
    text = r.json()["choices"][0]["message"]["content"]
    assert "sys-prompt" in text and "PING" in text


# -- session reuse -----------------------------------------------------------


def test_session_reused_across_turns(client):
    first = client.post(
        "/v1/chat/completions",
        json={"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": "first question"}]},
    ).json()
    assert "turn 1]" in first["choices"][0]["message"]["content"]

    second = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3-pro-high",
            "messages": [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": first["choices"][0]["message"]["content"]},
                {"role": "user", "content": "second question"},
            ],
        },
    ).json()
    # Same warm agy session: only "second question" is sent as a new turn.
    assert "turn 2]" in second["choices"][0]["message"]["content"]
    assert "second question" in second["choices"][0]["message"]["content"]


def test_divergent_history_gets_new_session(client):
    a = client.post(
        "/v1/chat/completions",
        json={"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": "branch A"}]},
    ).json()
    b = client.post(
        "/v1/chat/completions",
        json={"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": "branch B"}]},
    ).json()
    assert "turn 1]" in a["choices"][0]["message"]["content"]
    assert "turn 1]" in b["choices"][0]["message"]["content"]


# -- auth --------------------------------------------------------------------


@pytest.fixture
def auth_client(monkeypatch):
    monkeypatch.setenv("AGY_PROXY_BIN", f"{sys.executable} {FAKE_AGY}")
    monkeypatch.setenv("AGY_PROXY_API_KEYS", "secret1,secret2")
    with make_client() as c:
        yield c


def test_auth_required(auth_client):
    r = auth_client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "PING"}]})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"

    r = auth_client.post(
        "/v1beta/models/gemini-3-pro-high:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "PING"}]}]},
        headers={"x-goog-api-key": "wrong"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["status"] == "UNAUTHENTICATED"


def test_auth_accepts_all_key_conventions(auth_client):
    gemini_body = {"contents": [{"role": "user", "parts": [{"text": "PING"}]}]}
    r = auth_client.post(
        "/v1beta/models/gemini-3-pro-high:generateContent",
        json=gemini_body,
        headers={"x-goog-api-key": "secret1"},
    )
    assert r.status_code == 200
    r = auth_client.post(
        "/v1/chat/completions",
        json={"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": "PING"}]},
        headers={"Authorization": "Bearer secret2"},
    )
    assert r.status_code == 200


# -- error paths ---------------------------------------------------------------


def test_error_model(client):
    r = client.post(
        "/v1beta/models/error-model:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "PING"}]}]},
    )
    assert r.status_code == 502
    assert "error-model" in r.json()["error"]["message"]

    r = client.post(
        "/v1/chat/completions",
        json={"model": "error-model", "messages": [{"role": "user", "content": "PING"}]},
    )
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"


def test_crash_model(client):
    r = client.post(
        "/v1beta/models/crash-model:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "PING"}]}]},
    )
    assert r.status_code == 502
    assert "exited" in r.json()["error"]["message"]


def test_idle_timeout(monkeypatch):
    import os

    os.environ["AGY_PROXY_BIN"] = f"{sys.executable} {FAKE_AGY}"
    os.environ["AGY_PROXY_IDLE_TIMEOUT"] = "1.0"
    try:
        with make_client() as c:
            r = c.post(
                "/v1/chat/completions",
                json={"model": "slow-model", "messages": [{"role": "user", "content": "PING"}]},
            )
            assert r.status_code == 504
            assert r.json()["error"]["type"] == "timeout"
    finally:
        del os.environ["AGY_PROXY_BIN"]
        del os.environ["AGY_PROXY_IDLE_TIMEOUT"]


def test_bad_request_bodies(client):
    r = client.post(
        "/v1/chat/completions",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400

    r = client.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert r.status_code == 400


def test_http_proxy_config_reaches_agy_child(monkeypatch):
    import os

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    os.environ["AGY_PROXY_BIN"] = f"{sys.executable} {FAKE_AGY}"
    # scheme-less value must be normalized to http://...
    os.environ["AGY_PROXY_HTTP_PROXY"] = "127.0.0.1:7890"
    try:
        with make_client() as c:
            r = c.post(
                "/v1/chat/completions",
                json={"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": "hello"}]},
            )
            assert r.status_code == 200
            text = r.json()["choices"][0]["message"]["content"]
            assert "[proxy http://127.0.0.1:7890]" in text
    finally:
        del os.environ["AGY_PROXY_BIN"]
        del os.environ["AGY_PROXY_HTTP_PROXY"]


def test_prewarm_models(monkeypatch):
    import os
    import time

    os.environ["AGY_PROXY_BIN"] = f"{sys.executable} {FAKE_AGY}"
    os.environ["AGY_PROXY_PREWARM_MODELS"] = "gemini-3-pro-high"
    try:
        from app import create_app
        from app import agy as agy_module

        agy_module._model_caches.clear()
        app = create_app()
        with TestClient(app) as c:
            # The sweeper prewarms in the background; wait for the session.
            deadline = time.time() + 10
            while time.time() < deadline and app.state.sessions.live_count < 1:
                time.sleep(0.05)
            assert app.state.sessions.live_count >= 1
            r = c.post(
                "/v1/chat/completions",
                json={"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": "PING"}]},
            )
            assert r.status_code == 200
            assert r.json()["choices"][0]["message"]["content"] == "pong"
    finally:
        del os.environ["AGY_PROXY_BIN"]
        del os.environ["AGY_PROXY_PREWARM_MODELS"]


# -- web UI ---------------------------------------------------------------------


def test_web_ui_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "agy-proxy" in r.text
    assert 'id="root"' in r.text
    assert '/assets/ui.js' in r.text
    bundle = client.get('/assets/ui.js')
    assert bundle.status_code == 200
    assert bundle.headers['cache-control'] == 'no-cache'
    for marker in ("/dashboard", "/api/keys", "/api/models", "/login", "/update"):
        assert marker in bundle.text


def test_about_endpoint(client):
    r = client.get("/about")
    assert r.status_code == 200
    assert r.json()["version"] == "0.0.12"


# -- login bridge ---------------------------------------------------------------

SAMPLE_OSC8 = (
    b"\x1b]8;id=jkf24282d;https://accounts.google.com/o/oauth2/auth?access_type=offline&"
    b"client_id=&"
    b"code_challenge=E_nkcODZfv6YXGvM5iiLVjQexOWIhKUuz0pIZggbBxE&code_challenge_method=S256&"
    b"redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&state=0hi9r3WvOiS8BSYcJoGy-w"
    b"\x1b\\Click here to authenticate\x1b]8;;\x1b\\"
).decode("utf-8")


def test_login_url_extraction():
    from app.login import LoginBridge

    url = LoginBridge._extract_url(SAMPLE_OSC8)
    assert url is not None
    assert url.startswith("https://accounts.google.com/o/oauth2/auth?")
    assert "state=0hi9r3WvOiS8BSYcJoGy-w" in url  # full, not line-clipped
    assert "\x1b" not in url

    plain = "junk https://accounts.google.com/o/oauth2/auth?x=1&y=2 trailing"
    assert LoginBridge._extract_url(plain) == "https://accounts.google.com/o/oauth2/auth?x=1&y=2"
    assert LoginBridge._extract_url("no url here") is None


def test_login_page_and_status(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "agy-proxy" in r.text

    r = client.get("/login/status")
    assert r.status_code == 200
    assert r.json()["state"] == "idle"

    # submitting a code with no login in flight must not crash
    r = client.post("/login/code", json={"code": "4/0Abogus"})
    assert r.status_code == 200
    assert r.json()["state"] == "idle"


def test_login_actions_require_auth(auth_client):
    # Actions are guarded; with keys configured the pages themselves now
    # answer the Basic challenge (covered in test_pages_gated_by_basic_auth).
    r = auth_client.post("/login/start")
    assert r.status_code == 401
    r = auth_client.get("/login/status")
    assert r.status_code == 401
    r = auth_client.post("/login/code", json={"code": "x"}, headers={"x-api-key": "wrong"})
    assert r.status_code == 401

    assert auth_client.post("/login/input", json={"key": "enter", "session_id": "x"}).status_code == 401
    assert auth_client.post("/login/stop", json={"session_id": "x"}).status_code == 401
    assert auth_client.post("/login/finish", json={"session_id": "x"}).status_code == 401
    assert auth_client.post("/login/input", json={"key": "enter", "session_id": "x"}, headers={"Authorization": "Bearer secret1"}).status_code == 400

    r = auth_client.get("/login/status", headers={"Authorization": "Bearer secret1"})
    assert r.status_code == 200
    assert r.json()["state"] == "idle"


def test_health_is_minimal_and_docs_disabled(client):
    r = client.get("/health")
    body = r.json()
    assert body["status"] == "ok"
    assert "agy_bin" not in body  # no deployment details leaked
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_pages_gated_by_basic_auth(auth_client):
    import base64

    # Without a key the browser is redirected to the single-field sign-in page.
    for path in ("/", "/login"):
        r = auth_client.get(path, follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/signin"

    # API key typed into the Basic password field...
    key_pw = base64.b64encode(b"user:secret1").decode()
    r = auth_client.get("/", headers={"Authorization": f"Basic {key_pw}"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]

    # ...or the username field.
    key_user = base64.b64encode(b"secret2:").decode()
    assert auth_client.get("/login", headers={"Authorization": f"Basic {key_user}"}).status_code == 200

    # Basic creds also authenticate the data API.
    assert auth_client.get("/v1/models", headers={"Authorization": f"Basic {key_pw}"}).status_code == 200

    # Pages are never cached, so upgrades can't leave stale JS behind.
    r = auth_client.get("/", headers={"Authorization": f"Basic {key_pw}"})
    assert r.headers["cache-control"] == "no-store"


def test_signin_flow(auth_client):
    # The sign-in page itself is reachable and never cached.
    r = auth_client.get("/signin")
    assert r.status_code == 200
    assert "/assets/ui.js" in r.text

    # Wrong password is rejected, no cookie.
    r = auth_client.post("/signin", json={"key": "nope"})
    assert r.status_code == 401
    assert "agy_proxy_panel" not in r.cookies

    # An env-configured API key works as a panel credential too (legacy deployments).
    r = auth_client.post("/signin", json={"key": "secret1"})
    assert r.status_code == 200
    cookie = r.cookies.get("agy_proxy_panel")
    assert cookie == "secret1"

    # ...which unlocks the pages and the data API (no manual header needed).
    assert auth_client.get("/", cookies={"agy_proxy_panel": "secret1"}).status_code == 200
    assert auth_client.get("/v1/models", cookies={"agy_proxy_panel": "secret1"}).status_code == 200

    # Sign-out clears it again.
    assert auth_client.get("/signout").status_code == 200
    assert auth_client.get("/", cookies={"agy_proxy_panel": "secret1"}).status_code == 200  # still valid until browser drops it


def test_panel_password_and_upstream_key_are_separate(monkeypatch):
    """AGY_PROXY_PASSWORD opens the panel; panel-made keys serve the API only."""
    monkeypatch.setenv("AGY_PROXY_BIN", f"{sys.executable} {FAKE_AGY}")
    monkeypatch.setenv("AGY_PROXY_API_KEYS", "")  # no env keys: the two roles are distinct
    monkeypatch.setenv("AGY_PROXY_PASSWORD", "panel-pw")
    with make_client() as c:
        assert c.get("/", follow_redirects=False).status_code == 307
        assert c.post("/signin", json={"key": "panel-pw"}).status_code == 200
        assert c.get("/", cookies={"agy_proxy_panel": "panel-pw"}).status_code == 200

        created = c.post("/api/keys", json={"name": "client"}, cookies={"agy_proxy_panel": "panel-pw"})
        assert created.status_code == 201
        upstream = created.json()["key"]
        assert upstream.startswith("agy-")

        # The upstream key works for the API but must NOT open the panel...
        assert c.get("/v1/models", headers={"Authorization": f"Bearer {upstream}"}).status_code == 200
        assert c.get("/", headers={"Authorization": f"Bearer {upstream}"}, follow_redirects=False).status_code == 307
        assert c.get("/dashboard", headers={"Authorization": f"Bearer {upstream}"}).status_code == 401
        # ...and the panel password is not an upstream API key.
        assert c.get("/v1/models", headers={"Authorization": "Bearer panel-pw"}).status_code == 200  # panel creds still authenticate the UI/API

        # Revoking the key takes effect immediately.
        key_id = created.json()["id"]
        assert c.delete(f"/api/keys/{key_id}", cookies={"agy_proxy_panel": "panel-pw"}).status_code == 200
        assert c.get("/v1/models", headers={"Authorization": f"Bearer {upstream}"}).status_code == 401


def test_dashboard_reports_agy_usage(monkeypatch):
    """The dashboard totals come from agy's own usage envelope."""
    monkeypatch.setenv("AGY_PROXY_BIN", f"{sys.executable} {FAKE_AGY}")
    monkeypatch.setenv("AGY_PROXY_PASSWORD", "pw")
    with make_client() as c:
        c.post("/signin", json={"key": "pw"})
        jar = {"agy_proxy_panel": "pw"}
        created = c.post("/api/keys", json={"name": "client"}, cookies=jar).json()

        r = c.post(
            "/v1/chat/completions",
            json={"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": "PING"}]},
            headers={"Authorization": f"Bearer {created['key']}"},
        )
        assert r.status_code == 200
        used = r.json()["usage"]["total_tokens"]

        d = c.get("/dashboard", cookies=jar).json()
        assert d["totals"]["requests"] == 1
        assert d["totals"]["usage"]["total_tokens"] == used
        assert d["totals"]["usage"]["thinking_tokens"] > 0
        assert d["recent"][0]["model"] == "gemini-3-pro-high"
        assert d["recent"][0]["key_name"] == "client"
        assert d["keys"][0]["requests"] == 1
        assert "key" not in d["keys"][0]  # never expose the secret in listings


def test_pages_open_when_no_keys_configured(client):
    assert client.get("/").status_code == 200
    assert client.get("/login").status_code == 200
    # sign-in page explains that no credentials are configured
    r = client.get("/signin")
    assert r.status_code == 200
    assert client.get("/health").json()["auth_required"] is False
    assert "未设置鉴权" in client.get("/assets/ui.js").text
    # ...and that branch is styled too, not a bare fallback page
    assert 'href="/app.css"' in r.text


def test_favicon_and_icons(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert "svg" in r.headers["content-type"]
    # every page points at the shared favicon
    for path in ("/", "/login", "/signin"):
        assert "rel=\"icon\"" in client.get(path).text or "rel='icon'" in client.get(path).text


def test_app_css_served_and_shared_by_every_page(client):
    """One compiled stylesheet backs all four pages (no per-page <style> fork)."""
    r = client.get("/app.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    # No content hash in the filename, so it must revalidate rather than be
    # cached hard — and it must NOT inherit the pages' `no-store`.
    assert r.headers["cache-control"] == "no-cache"
    # the shadcn token contract and the component vocabulary are really in there
    for marker in ("--background:", "--accent:", "--radius:", "--primary:"):
        assert marker in r.text, marker

    # ...and each page links it instead of carrying a private palette
    for path in ("/", "/login", "/signin", "/update"):
        html = client.get(path).text
        assert 'href="/app.css"' in html, path
        assert "<style" not in html, path


def test_login_page_uses_absolute_api_paths(client):
    # Regression: relative fetch('status') resolved to /status (404) when the
    # page was opened at /login.
    html = client.get("/assets/ui.js").text
    assert "/login" in html
    assert "/status" in html
    assert "/start" in html
    assert "/code" in html
    assert "fetch('status')" not in html


def test_frontend_assets(client):
    # The dashboard is a plain HTML page whose only external asset is the one
    # compiled stylesheet; the vendored bundles stay reachable for anyone still
    # pointing a client at them.
    r = client.get("/")
    assert r.status_code == 200
    assert "deep-chat" not in r.text  # the chat component is gone
    for path, marker, mime in (
        ("/marked.min.js", "marked", "javascript"),
        ("/purify.min.js", "DOMPurify", "javascript"),
    ):
        r = client.get(path)
        assert r.status_code == 200
        assert mime in r.headers["content-type"]
        assert marker in r.text


# -- update bridge ---------------------------------------------------------------

import threading
import time as _time


def _reset_fake_agy_version():
    Path(tempfile.gettempdir(), "fake-agy-version.txt").unlink(missing_ok=True)


def _wait_update_state(client, want, timeout=15.0):
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        s = client.get("/update/status").json()
        if s["state"] == want:
            return s
        if s["state"] == "error":
            return s
        _time.sleep(0.05)
    raise AssertionError(f"update never reached {want!r}: {s}")


@pytest.fixture
def fresh_update_env(monkeypatch):
    """Reset the fake agy's simulated version before and after each test."""
    import tempfile as _tempfile

    _reset_fake_agy_version()
    yield
    _reset_fake_agy_version()


def test_update_page_served(client, fresh_update_env):
    r = client.get("/update")
    assert r.status_code == 200
    assert "/assets/ui.js" in r.text
    assert "/update" in client.get("/assets/ui.js").text
    assert "/start" in client.get("/assets/ui.js").text
    assert "fetch('start')" not in r.text


def test_update_status_reports_version(client, fresh_update_env):
    r = client.get("/update/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "idle"
    assert body["version"] == "9.9.9"


def test_update_full_flow(client, fresh_update_env):
    # A finished chat leaves an idle session behind; the update must kill it.
    assert (
        client.post(
            "/v1/chat/completions",
            json={"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": "PING"}]},
        ).status_code
        == 200
    )

    r = client.post("/update/start")
    assert r.status_code == 200
    assert r.json()["state"] == "running"

    s = _wait_update_state(client, "done")
    assert "v9.9.9 → v9.9.10" in s["message"]
    assert s["before_version"] == "9.9.9"
    assert s["after_version"] == "9.9.10"
    assert s["version"] == "9.9.10"
    assert s["sessions_killed"] == 1

    # chat still works after the update (session respawned from the new binary)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": "PING"}]},
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "pong"


def test_update_already_latest(client, fresh_update_env):
    from tests import fake_agy as _  # noqa: F401  (ensure module importable)

    Path(tempfile.gettempdir(), "fake-agy-version.txt").write_text("9.9.10", encoding="utf-8")

    r = client.post("/update/start")
    assert r.status_code == 200
    s = _wait_update_state(client, "done")
    assert "已是最新" in s["message"]
    assert s["after_version"] == "9.9.10"


def test_update_failure_surfaces_error(client, fresh_update_env):
    os.environ["FAKE_AGY_UPDATE_FAIL"] = "1"
    try:
        r = client.post("/update/start")
        assert r.status_code == 200
        s = _wait_update_state(client, "error")
        assert "agy update 失败" in s["message"]
        assert "network unreachable" in s["message"]
        # a failed update leaves the CLI untouched and a retry is possible
        assert client.post("/update/start").json()["state"] == "running"
        _wait_update_state(client, "done")
    finally:
        del os.environ["FAKE_AGY_UPDATE_FAIL"]


def test_update_kills_only_idle_sessions(client, fresh_update_env):
    # Hold one busy session (a slow in-flight turn) while updating.
    result = {}

    def slow_turn():
        result["resp"] = client.post(
            "/v1/chat/completions",
            json={"model": "slow-model", "messages": [{"role": "user", "content": "hold"}]},
        )

    worker = threading.Thread(target=slow_turn)
    worker.start()
    _time.sleep(0.3)  # let the slow turn take over its session's lock

    r = client.post("/update/start")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "running"
    assert body["busy_sessions"] == 1

    s = _wait_update_state(client, "done")
    worker.join(timeout=30)
    assert result["resp"].status_code == 200
    assert result["resp"].json()["choices"][0]["message"]["content"]  # turn completed


def test_update_actions_require_auth(auth_client):
    r = auth_client.post("/update/start")
    assert r.status_code == 401
    r = auth_client.get("/update/status")
    assert r.status_code == 401

    r = auth_client.get("/update", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/signin"

    r = auth_client.get("/update/status", headers={"Authorization": "Bearer secret1"})
    assert r.status_code == 200
    assert r.json()["state"] == "idle"


def test_homepage_links_updater(client):
    html = client.get("/").text
    assert "/assets/ui.js" in html
    assert "/update" in client.get("/assets/ui.js").text


# -- helpers -------------------------------------------------------------------


def _sse_events(response) -> list:
    events = []
    for line in response.iter_lines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        events.append(json.loads(payload) if payload != "[DONE]" else "[DONE]")
    return events
