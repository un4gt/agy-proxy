"""Account identities, persistent affinity and quota failover through real children."""
import asyncio
import json
import os
import sys
import time
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.account_pool import AccountPool, AccountPoolError, native_credentials, quota_exhausted
from app.agy import AgyError, SessionManager, _child_env, list_models, _model_caches
from app.config import Config
from app.runner import run_turn
from app.session_context import SessionIdentity


def credentials(label):
    return {"token": {"access_token": f"PRIVATE_{label}", "refresh_token": f"REFRESH_{label}",
                      "expiry": "2099-01-01T00:00:00Z"}, "auth_method": "consumer"}


@pytest.fixture
def pool_setup(tmp_path, monkeypatch):
    fake = (Path(__file__).parent / "fake_tools_agy.py").as_posix()
    cfg = Config(agy_bin=f"{sys.executable} {fake}", data_dir=str(tmp_path / "data"))
    pool = cfg.account_pool = AccountPool(cfg)
    a = pool.create("A", credentials("A"))
    b = pool.create("B", credentials("B"))
    trace = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_AGY_ACCOUNT_LOG", str(trace))
    return cfg, pool, a, b, trace


def fail(profile, message="QUOTA_EXHAUSTED: weekly quota exhausted", **options):
    (profile.home / "upstream-error.json").write_text(json.dumps({"message": message, **options}))


def attempts(trace):
    return [json.loads(line) for line in trace.read_text().splitlines()]


async def turn(cfg, mgr, text="PING", *, history=None, owner="client", model="auto", key=None):
    async with aclosing(run_turn(cfg, mgr, system="", history=history or [], final_user=text,
                                 model=model, effort=None, session_key=SessionIdentity(owner, key))) as events:
        return [value async for kind, value in events if kind == "result"][0]


def test_profiles_and_child_state_are_distinct_and_stable(pool_setup):
    cfg, pool, a, b, trace = pool_setup
    host_env = dict(os.environ)
    (a.home / ".gemini/state.json").write_text('{"only_a":true}')
    assert a.home != b.home
    assert (a.home.parent / "state.json").read_bytes() != (b.home.parent / "state.json").read_bytes()
    assert _child_env(a.configured(cfg))["HOME"] == str(a.home)
    assert _child_env(a.configured(cfg))["USERPROFILE"] == str(a.home)
    assert os.environ == host_env

    async def scenario():
        mgr = SessionManager(cfg)
        try:
            response_a = json.loads((await turn(cfg, mgr, "INSPECT_ACCOUNT")).text)
            pool.mark_exhausted(a.id, "auto")
            response_b = json.loads((await turn(cfg, mgr, "INSPECT_ACCOUNT")).text)
            assert response_a["token"] == "PRIVATE_A" and response_b["token"] == "PRIVATE_B"
            assert response_a["state"] == {"only_a": True} and response_b["state"] == {}
            assert response_a["installation_id"] != response_b["installation_id"]
            await mgr.close_all()
            assert json.loads((await turn(cfg, mgr, "INSPECT_ACCOUNT")).text) == response_b
        finally:
            await mgr.close_all()
    asyncio.run(scenario())
    assert pool.select("client").id == b.id
    reloaded = AccountPool(cfg)
    assert reloaded.select("client").id == b.id
    assert reloaded.get(a.id).home == a.home
    assert pool.create("same account", credentials("A")).id == a.id
    assert len(pool.profiles()) == 2


@pytest.mark.parametrize("initial", [None, "OLD"])
def test_reimport_after_native_login_or_refresh_keeps_identity(tmp_path, initial):
    cfg = Config(data_dir=str(tmp_path))
    pool = AccountPool(cfg)
    profile = pool.create("A", credentials(initial) if initial else None)
    identity = (profile.home.parent / "state.json").read_bytes()
    # The native CLI writes the first login or rotates tokens outside the pool.
    profile.credentials.write_text(json.dumps(credentials("CURRENT")))
    pool.select("client", "gemini-pro")
    pool.mark_exhausted(profile.id, "claude-sonnet")
    pool = AccountPool(cfg)
    assert pool.create("reimported A", credentials("CURRENT")).id == profile.id
    assert len(pool.profiles()) == 1
    assert (profile.home.parent / "state.json").read_bytes() == identity
    assert pool.select("client", "gemini-pro").id == profile.id
    assert pool.public(profile.id)["exhausted_scopes"] == ["third-party"]


def test_child_auth_overrides_do_not_escape_account_home(pool_setup, monkeypatch):
    cfg, _, a, _, _ = pool_setup
    for name in ("AGY_ADC_AUTH", "GOOGLE_APPLICATION_CREDENTIALS", "JETSKI_OAUTH_TOKEN",
                 "GEMINI_API_KEY", "AGY_LLM_GATEWAY_API_KEY", "ANTIGRAVITY_LS_ADDRESS"):
        monkeypatch.setenv(name, "host-secret")
    monkeypatch.setenv("SSH_CONNECTION", "")
    cfg.http_proxy = "http://proxy.invalid:7890"
    env = _child_env(a.configured(cfg))
    assert "host-secret" not in env.values()
    assert env["SSH_CONNECTION"] and env["HTTPS_PROXY"] == cfg.http_proxy
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                 "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "TMP", "TEMP", "TMPDIR"):
        assert Path(env[name]).is_relative_to(a.home)


def test_affinity_survives_model_change_reclamation_and_restart(pool_setup):
    cfg, pool, a, _, trace = pool_setup
    async def scenario():
        mgr = SessionManager(cfg)
        try:
            first = await turn(cfg, mgr)
            await mgr.close_idle()
            second = await turn(cfg, mgr, model="gemini-3-pro-high", key="explicit")
            assert first.conversation_id != second.conversation_id
            for session in mgr._sessions:
                session.last_used -= cfg.session_ttl + 1
            await mgr.sweep()
            await turn(cfg, mgr, model="gemini-3-pro-low", key="explicit")
        finally:
            await mgr.close_all()
    asyncio.run(scenario())
    assert {r["home"] for r in attempts(trace)} == {str(a.home)}
    cfg.account_pool = AccountPool(cfg)
    asyncio.run(scenario())
    assert {r["home"] for r in attempts(trace)} == {str(a.home)}


def test_quota_failover_replays_full_history_and_keeps_new_account(pool_setup):
    cfg, pool, a, b, trace = pool_setup
    fail(a, trigger="NEXT")
    async def scenario():
        mgr = SessionManager(cfg)
        try:
            first = await turn(cfg, mgr, "FIRST", key="conversation")
            history = [("user", "FIRST"), ("assistant", first.text)]
            second = await turn(cfg, mgr, "NEXT", history=history, key="conversation")
            assert second.conversation_id != first.conversation_id and second.num_turns == 1
            records = attempts(trace)
            assert [r["home"] for r in records] == [str(a.home), str(a.home), str(b.home)]
            assert "[User]\nFIRST" not in records[1]["prompt"]  # same PID continuation
            assert "[User]\nFIRST" in records[2]["prompt"]  # full replay in B
            # A recovering is not a reason to move a healthy B binding.
            pool.data["exhausted"].clear()
            await turn(cfg, mgr, "AFTER")
            assert attempts(trace)[-1]["home"] == str(b.home)
        finally:
            await mgr.close_all()
    asyncio.run(scenario())


@pytest.mark.parametrize("message", ["HTTP 429: Too many requests", "RESOURCE_EXHAUSTED", "UNAUTHENTICATED",
                                     "upstream unavailable", "timeout", "Quota exceeded for requests per minute"])
def test_non_quota_failures_never_switch_accounts(pool_setup, message):
    cfg, pool, a, _, trace = pool_setup
    fail(a, message)
    async def scenario():
        mgr = SessionManager(cfg)
        try:
            for _ in range(2):
                with pytest.raises(AgyError):
                    await turn(cfg, mgr)
            assert pool.select("client").id == a.id
            assert len(attempts(trace)) == 2
            assert {r["home"] for r in attempts(trace)} == {str(a.home)}
        finally:
            await mgr.close_all()
    asyncio.run(scenario())


@pytest.mark.parametrize("option", ["partial", "tool"])
def test_no_replay_after_partial_output_or_native_tool_execution(pool_setup, option):
    cfg, pool, a, b, trace = pool_setup
    fail(a, **{option: True})
    async def scenario():
        mgr = SessionManager(cfg)
        try:
            with pytest.raises(AgyError) as exc:
                await turn(cfg, mgr)
            assert exc.value.status_code == 429
            assert len(attempts(trace)) == 1
            assert pool.select("client", "auto").id == b.id
            assert (await turn(cfg, mgr)).text == "pong"
        finally:
            await mgr.close_all()
    asyncio.run(scenario())


def test_all_accounts_exhausted_and_parallel_failures_are_bounded(pool_setup):
    cfg, pool, a, b, trace = pool_setup
    fail(a)
    async def scenario():
        mgr = SessionManager(cfg)
        try:
            replies = await asyncio.gather(*[turn(cfg, mgr) for _ in range(3)])
            assert all(r.text == "pong" for r in replies)
            assert pool.select("client").id == b.id
            assert len([r for r in attempts(trace) if r["home"] == str(b.home)]) == 3
            fail(b)
            with pytest.raises(AgyError) as exc:
                await turn(cfg, mgr)
            assert exc.value.status_code == 429
            count = len(attempts(trace))
            with pytest.raises(AgyError):
                await turn(cfg, mgr)
            assert len(attempts(trace)) == count
        finally:
            await mgr.close_all()
    asyncio.run(scenario())


def test_busy_capacity_does_not_select_another_account(pool_setup):
    cfg, pool, a, _, _ = pool_setup
    cfg.max_sessions = 1
    async def scenario():
        mgr = SessionManager(cfg)
        try:
            await mgr.acquire(model="auto", effort=None)
            with pytest.raises(AgyError) as exc:
                await mgr.acquire(model="auto", effort=None)
            assert exc.value.status_code == 429 and pool.select().id == a.id
        finally:
            await mgr.close_all()
    asyncio.run(scenario())


def test_missing_credentials_or_identity_must_not_fall_back(pool_setup):
    cfg, pool, a, _, _ = pool_setup
    assert pool.select().id == a.id
    a.credentials.unlink()
    with pytest.raises(AgyError, match="凭据缺失"):
        pool.select()
    a.credentials.write_text(json.dumps(credentials("A")))
    (a.home.parent / "state.json").write_text("broken")
    async def scenario():
        mgr = SessionManager(cfg)
        try:
            with pytest.raises(AgyError, match="state.json"):
                await mgr.acquire(model="auto", effort=None)
            assert mgr.live_count == 0
        finally:
            await mgr.close_all()
    asyncio.run(scenario())
    assert pool.select().id == a.id


def test_quota_observation_requires_zero_and_is_scoped_and_fresh(pool_setup):
    _, pool, a, b, _ = pool_setup
    assert pool.select("owner", "gemini-3-pro-high").id == a.id
    def usage(value, when=None, reset=None, name="Gemini Models"):
        return {"fetched_at": when or time.time(), "groups": [{"name": name, "models": "",
                "buckets": [{"remaining_fraction": value, "reset_at": reset}]}]}
    for value in (None, 0.001, float("nan"), False, -1):
        pool.observe_quota(a.id, usage(value))
        assert pool.select("owner", "gemini-3-pro-high").id == a.id
    pool.observe_quota(a.id, usage(0, when=time.time() - 120))
    pool.observe_quota(a.id, usage(0, reset=time.time() - 1))
    pool.observe_quota(a.id, usage(0, name="Unknown models"))
    assert pool.select("owner", "gemini-3-pro-high").id == a.id
    pool.observe_quota(a.id, usage(0, reset=time.time() + 3600))
    assert pool.select("another-owner", "claude-opus").id == a.id
    assert pool.select("owner", "gemini-3-pro-low").id == b.id
    pool.observe_quota(a.id, usage(1))
    assert pool.select("owner", "gemini-3-pro-low").id == b.id
    assert pool.select("new-owner", "gemini-3-pro-low").id == a.id


@pytest.mark.parametrize("protocol", ["openai", "gemini", "responses", "anthropic", "structured"])
@pytest.mark.parametrize("stream", [False, True])
def test_quota_retry_reaches_all_protocols(pool_setup, protocol, stream):
    cfg, _, a, b, trace = pool_setup
    fail(a, stderr=(protocol == "gemini"))
    app = create_app(cfg)
    paths = {"openai": "/v1/chat/completions", "structured": "/v1/chat/completions",
             "gemini": "/v1beta/models/auto:" + ("streamGenerateContent" if stream else "generateContent"),
             "responses": "/v1/responses", "anthropic": "/v1/messages"}
    body = {"model": "auto", "messages": [{"role": "user", "content": "PING"}], "stream": stream}
    if protocol == "gemini":
        body = {"contents": [{"role": "user", "parts": [{"text": "PING"}]}]}
    elif protocol == "responses":
        body = {"model": "auto", "input": "PING", "stream": stream}
    elif protocol == "anthropic":
        body["max_tokens"] = 100
    elif protocol == "structured":
        body["response_format"] = {"type": "json_object"}
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post(paths[protocol], json=body)
            assert response.status_code == 200, response.text
            assert "QUOTA_EXHAUSTED" not in response.text and "PRIVATE" not in response.text
    assert [r["home"] for r in attempts(trace)] == [str(a.home), str(b.home), str(b.home)]


def test_models_cache_uses_account_identity(pool_setup):
    cfg, pool, a, b, _ = pool_setup
    _model_caches.clear()
    async def scenario():
        await list_models(cfg, owner="owner", model="auto")
        pool.mark_exhausted(a.id, "auto")
        await list_models(cfg, owner="owner", model="auto")
    asyncio.run(scenario())
    assert (cfg.agy_bin, str(a.home)) in _model_caches
    assert (cfg.agy_bin, str(b.home)) in _model_caches


def test_panel_import_auth_and_per_account_quota(tmp_path, monkeypatch):
    from app import account
    monkeypatch.setattr(account, "credentials_path", lambda: tmp_path / "no-host-credentials")
    cfg = Config(password="panel", data_dir=str(tmp_path / "data"))
    app = create_app(cfg)
    client = TestClient(app)
    assert client.post("/api/accounts", json={"name": "A"}).status_code == 401
    client.post("/signin", json={"key": "panel"})
    ids = [client.post("/api/accounts", json={"name": label, "credentials": credentials(label)}).json()["id"]
           for label in ("A", "B")]
    assert len(set(ids)) == 2
    assert "PRIVATE" not in app.state.account_pool.path.read_text()
    cls = httpx.AsyncClient
    def handler(request):
        if request.url.path.endswith("loadCodeAssist"):
            return httpx.Response(200, json={"cloudaicompanionProject": "project"})
        return httpx.Response(200, json={"groups": [{"displayName": "Gemini", "buckets": [{
            "remainingFraction": 0 if request.headers["authorization"] == "Bearer PRIVATE_A" else 1}]}]})
    monkeypatch.setattr(account.httpx, "AsyncClient", lambda **kw: cls(transport=httpx.MockTransport(handler), **kw))
    response = client.get("/api/accounts")
    assert response.status_code == 200 and "PRIVATE" not in response.text and "REFRESH" not in response.text
    assert len(response.json()["accounts"]) == 2
    for account_id in ids:
        assert client.get(f"/api/accounts/{account_id}/usage").json()["id"] == account_id
        assert client.get(f"/login/status?account_id={account_id}").status_code == 200
    bridges = app.state.account_login_bridges
    assert bridges[ids[0]]._credentials_path() != bridges[ids[1]]._credentials_path()
    assert client.get("/login/status?account_id=missing").status_code == 404
    assert client.get("/api/accounts/current/usage").status_code == 404
    assert app.state.account_pool.select("new", "gemini-3-pro-high").id == ids[1]
    bad = client.post("/api/accounts", json={"name": "bad", "credentials": {"token": "PRIVATE_INVALID"}})
    assert bad.status_code == 400 and "PRIVATE_INVALID" not in bad.text


def test_adding_b_preserves_existing_a_and_its_machine_identity(tmp_path, monkeypatch):
    from app import account
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    native = tmp_path / ".gemini/antigravity-cli"
    native.mkdir(parents=True)
    original = json.dumps(credentials("A"))
    (native / "antigravity-oauth-token").write_text(original)
    machine_id = "6c672e7e-7745-4a25-888a-e6513585ff66"
    (native / "installation_id").write_text(machine_id)
    app = create_app(Config(data_dir=str(tmp_path / "data")))
    client = TestClient(app)
    response = client.post("/api/accounts", json={"name": "B", "credentials": credentials("B")})
    assert response.status_code == 201
    pool = app.state.account_pool
    assert len(pool.profiles()) == 2
    a = pool.select("client")
    assert a.id != response.json()["id"]
    assert json.loads(a.credentials.read_bytes())["token"]["access_token"] == "PRIVATE_A"
    assert (a.home / ".gemini/antigravity-cli/installation_id").read_text() == machine_id
    assert account.credentials_path().read_text() == original


def test_persistence_failure_does_not_rebind_in_memory(pool_setup, monkeypatch):
    import app.account_pool as module
    _, pool, a, _, _ = pool_setup
    assert pool.select("owner").id == a.id
    pool.mark_exhausted(a.id, "auto")
    before = json.loads(pool.path.read_bytes())
    monkeypatch.setattr(module, "write_private", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(AccountPoolError):
        pool.select("owner", "auto")
    assert pool.data == before


def test_reference_token_format_and_corrupt_index(tmp_path):
    payload = native_credentials({"token": {"access_token": "a", "refresh_token": "r", "expiry_timestamp": 1893456000}})
    assert payload["token"]["expiry"] == "2030-01-01T00:00:00+00:00"
    (tmp_path / "accounts.json").write_text('{"accounts":[]}')
    with pytest.raises(AccountPoolError, match="accounts.json"):
        AccountPool(Config(data_dir=str(tmp_path)))
    assert not quota_exhausted("429 RESOURCE_EXHAUSTED")
    assert quota_exhausted("You have exhausted your weekly quota.")
    assert quota_exhausted("HTTP 429 Too many requests: You have exhausted your quota.")


def test_import_current_does_not_confuse_gemini_credentials_with_agy(tmp_path, monkeypatch):
    from app import account
    path = tmp_path / "oauth_creds.json"
    path.write_text(json.dumps(credentials("UNRELATED_GEMINI")))
    monkeypatch.setattr(account, "credentials_path", lambda: path)
    app = create_app(Config(data_dir=str(tmp_path / "data")))
    response = TestClient(app).post("/api/accounts", json={"name": "A", "import_current": True})
    assert response.status_code == 400
    assert not app.state.account_pool.enabled


def test_rename_preserves_identity_credentials_quota_and_bindings(pool_setup):
    cfg, pool, a, b, _ = pool_setup
    pool.select("client-a")
    pool.mark_exhausted(a.id, "gemini")
    pool.select("client-b", "gemini")
    before = json.loads(pool.path.read_bytes())
    identity = (a.home.parent / "state.json").read_bytes()
    token = a.credentials.read_bytes()
    assert pool.rename(a.id, "  主账号  ")["name"] == "主账号"
    reopened = AccountPool(cfg)
    assert reopened.get(a.id).name == "主账号"
    assert reopened.get(b.id).name == "B"
    for field in ("bindings", "exhausted"):
        assert reopened.data[field] == before[field]
    assert (a.home.parent / "state.json").read_bytes() == identity
    assert a.credentials.read_bytes() == token


@pytest.mark.parametrize("operation", ["rename", "remove"])
def test_account_edit_disk_failure_preserves_account(pool_setup, monkeypatch, operation):
    import app.account_pool as module
    cfg, pool, a, _, _ = pool_setup
    pool.select("client")
    before = pool.path.read_bytes()
    token = a.credentials.read_bytes()
    monkeypatch.setattr(module, "write_private", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(AccountPoolError):
        if operation == "rename":
            pool.rename(a.id, "changed")
        else:
            pool.remove(a.id)
    assert pool.data == json.loads(before)
    assert pool.path.read_bytes() == before
    assert a.credentials.read_bytes() == token
    assert AccountPool(cfg).select("client").id == a.id


def test_delete_clears_only_selected_account_and_its_bindings(pool_setup):
    cfg, pool, a, b, _ = pool_setup
    assert pool.select("client-a").id == a.id
    pool.mark_exhausted(a.id, "gemini")
    assert pool.select("client-b", "gemini").id == b.id
    b_token = b.credentials.read_bytes()
    result = pool.remove(a.id)
    assert result == {"deleted": a.id, "released_clients": 1}
    assert not a.home.parent.exists()
    assert b.credentials.read_bytes() == b_token
    assert a.id not in pool.data["exhausted"]
    reopened = AccountPool(cfg)
    assert reopened.select("client-a").id == b.id
    assert reopened.select("client-b").id == b.id


def test_delete_last_account_never_reactivates_legacy_login(pool_setup, monkeypatch, tmp_path):
    from app import account
    cfg, pool, a, b, _ = pool_setup
    legacy = tmp_path / "antigravity-oauth-token"
    legacy.write_text(json.dumps(credentials("LEGACY")))
    monkeypatch.setattr(account, "credentials_path", lambda: legacy)
    pool.remove(a.id)
    pool.remove(b.id)
    app = create_app(cfg)
    client = TestClient(app)
    assert app.state.account_pool.enabled
    assert client.get("/health").json()["signed_in"] is False
    assert client.get("/api/accounts").json() == {"accounts": []}
    with pytest.raises(AgyError):
        app.state.account_pool.select("client")
    assert client.get("/login/status").status_code == 404
    response = client.post("/api/accounts", json={"name": "new"})
    assert response.status_code == 201
    assert [p.name for p in app.state.account_pool.profiles()] == ["new"]
    assert legacy.exists()


def test_account_mutations_require_panel_auth_and_validate_names(tmp_path, monkeypatch):
    from app import account
    monkeypatch.setattr(account, "credentials_path", lambda: tmp_path / "missing")
    app = create_app(Config(password="panel", data_dir=str(tmp_path / "data")))
    profile = app.state.account_pool.create("A", credentials("A"))
    client = TestClient(app)
    path = f"/api/accounts/{profile.id}"
    assert client.patch(path, json={"name": "new"}).status_code == 401
    assert client.delete(path).status_code == 401
    client.post("/signin", json={"key": "panel"})
    for body in ({"name": ""}, {"name": "  "}, {"name": "a" * 101}, {"name": 3}, [],
                 {"name": "new", "machine_id": "change-not-allowed"}):
        assert client.patch(path, json=body).status_code == 400
        assert app.state.account_pool.get(profile.id).name == "A"
    response = client.patch(path, json={"name": "主账号"})
    assert response.status_code == 200 and response.json()["name"] == "主账号"
    assert "PRIVATE" not in response.text
    assert client.delete(path).status_code == 200
    assert client.delete(path).status_code == 404
    assert client.patch(path, json={"name": "new"}).status_code == 404
    assert client.delete("/api/accounts/current").status_code == 404


def test_delete_refuses_busy_or_logging_in_account_then_closes_idle_process(pool_setup):
    from app.login import LoginBridge
    cfg, pool, a, b, _ = pool_setup
    async def scenario():
        mgr = SessionManager(cfg)
        try:
            session = await mgr.acquire(model="auto", effort=None)
            assert session.account_id == a.id and session.busy
            with pytest.raises(AgyError) as busy:
                await mgr.remove_account(a.id)
            assert busy.value.status_code == 409
            assert session.alive and a.credentials.exists()
            session.leased = False
            bridge = LoginBridge(a.configured(cfg))
            bridge.state = "waiting_code"
            with pytest.raises(AgyError) as login:
                await mgr.remove_account(a.id, login_bridges={a.id: bridge})
            assert login.value.status_code == 409
            bridge.state = "idle"
            result = await mgr.remove_account(a.id, login_bridges={a.id: bridge})
            assert result["deleted"] == a.id
            assert not session.alive and mgr.live_count == 0
            assert pool.select().id == b.id
        finally:
            await mgr.close_all()
    asyncio.run(scenario())


def test_delete_checks_both_session_pools_before_removing_credentials(pool_setup):
    cfg, pool, account, _, _ = pool_setup

    async def scenario():
        standard, native = SessionManager(cfg), SessionManager(cfg)
        try:
            idle = await standard.acquire(model="auto", effort=None)
            idle.leased = False
            active = await native.acquire(model="auto", effort=None)
            with pytest.raises(AgyError) as error:
                await standard.remove_account(account.id, other_managers=(native,))
            assert error.value.status_code == 409
            assert idle.alive and active.alive and account.credentials.exists()
            active.leased = False
            await standard.remove_account(account.id, other_managers=(native,))
            assert not idle.alive and not active.alive
            assert standard.live_count == native.live_count == 0
            assert not account.credentials.exists()
        finally:
            await standard.close_all()
            await native.close_all()

    asyncio.run(scenario())


def test_deletion_blocks_new_login_and_discovery_until_sessions_close(pool_setup, monkeypatch):
    cfg, pool, a, _, _ = pool_setup
    async def scenario():
        mgr = SessionManager(cfg)
        session = await mgr.acquire(model="auto", effort=None)
        session.leased = False
        closing, finish = asyncio.Event(), asyncio.Event()
        close = session.close
        async def delayed_close():
            closing.set()
            await finish.wait()
            await close()
        monkeypatch.setattr(session, "close", delayed_close)
        task = asyncio.create_task(mgr.remove_account(a.id))
        try:
            await asyncio.wait_for(closing.wait(), timeout=5)
            with pytest.raises(AccountPoolError, match="正在删除"):
                pool.prepare(a)
            with pytest.raises(AgyError, match="正在删除"):
                pool.select()
        finally:
            finish.set()
            await task
            await mgr.close_all()
    asyncio.run(scenario())


def test_account_list_tolerates_concurrent_deletion(tmp_path, monkeypatch):
    from app import account
    app = create_app(Config(data_dir=str(tmp_path / "data")))
    profile = app.state.account_pool.create("A", credentials("A"))
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        async def snapshot(self, refresh=False):
            entered.set()
            await finish.wait()
            return {"id": profile.id, "status": "connected", "usage": None}
        monkeypatch.setattr(account.AccountService, "snapshot", snapshot)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            listing = asyncio.create_task(client.get("/api/accounts"))
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert (await client.delete(f"/api/accounts/{profile.id}")).status_code == 200
            finish.set()
            response = await listing
            assert response.status_code == 200 and response.json() == {"accounts": []}
    asyncio.run(scenario())


def test_delete_reports_cleanup_failure_without_reactivating_account(pool_setup, monkeypatch):
    import app.account_pool as module
    cfg, pool, a, b, _ = pool_setup
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_: (_ for _ in ()).throw(PermissionError()))
    result = pool.remove(a.id)
    assert result["warning"] and a.credentials.exists()
    assert AccountPool(cfg).select("client").id == b.id
