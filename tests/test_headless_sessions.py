"""Official headless protocol, persistent sessions, and HTTP liveness regressions."""
import asyncio
import json
import sys
import time
from contextlib import aclosing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import create_app
from app.agy import AgyError, SessionManager
from app.config import Config
from app.openai_api import chat_completions
from app.runner import DeltaTracker, run_turn
from app.session_context import SessionContext, SessionIdentity


def config(tmp_path=None, *, tools=False, **kw):
    fake = Path(__file__).with_name("fake_tools_agy.py" if tools else "fake_agy.py").as_posix()
    return Config(agy_bin=f"{sys.executable} {fake}", data_dir=str(tmp_path) if tmp_path else None, **kw)


def test_done_is_an_incremental_tail_including_repeated_text():
    tracker = DeltaTracker()
    assert tracker.feed({"step_type": "tool", "text_delta": "must not leak"}) is None
    fragments = [tracker.feed({"step_type": "agent_response", "step_index": 2,
                              "state": state, "text_delta": text})
                 for state, text in [("ACTIVE", "哈"), ("DONE", "哈\n")]]
    assert fragments == ["哈", "哈\n"]
    assert tracker.streamed == "哈哈\n"


def test_two_stdin_turns_have_one_init_and_per_turn_usage():
    async def check():
        mgr = SessionManager(config())
        try:
            session = await mgr.acquire(model="auto", effort=None)
            rounds = []
            for _ in range(2):
                async with aclosing(session.send_turn("PING")) as events:
                    rounds.append([e async for e in events])
            assert sum(e["event"] == "init" for events in rounds for e in events) == 1
            first, second = [events[-1]["result"] for events in rounds]
            assert first["conversation_id"] == second["conversation_id"]
            assert [first["num_turns"], second["num_turns"]] == [1, 2]
            assert first["usage"] == second["usage"]
        finally:
            await mgr.close_all()
    asyncio.run(check())


@pytest.fixture
def client(tmp_path):
    cfg = config(tmp_path, tools=True, api_keys=("owner-a", "owner-b"), stream_heartbeat=0.05)
    with TestClient(create_app(cfg)) as client:
        yield client


def headers(session="chat-a", owner="owner-a"):
    return {"Authorization": f"Bearer {owner}", "X-Session-Affinity": session}


def inspect(client, history, **kw):
    body = dict(model="auto", tools=[], messages=history)
    body.update(kw.pop("body", {}))
    response = client.post("/v1/chat/completions", json=body, headers=kw.pop("headers", headers()), **kw)
    assert response.status_code == 200, response.text
    message = response.json()["choices"][0]["message"]
    return message, json.loads(message["content"]), response.json()["usage"]


def test_identified_session_reuses_pid_and_only_submits_new_messages(client):
    initial = [{"role": "user", "content": "INSPECT_CONTEXT first"}]
    reply, first, usage = inspect(client, initial)
    history = initial + [reply, {"role": "user", "content": "INSPECT_CONTEXT second"}]
    _, second, next_usage = inspect(client, history)
    assert first["pid"] == second["pid"] and second["turn"] == 2
    assert second["history"] == history
    assert client.app.state.sessions.live_count == 1
    assert client.app.state.sessions.busy_count() == 0
    # Returned usage is for this turn, while the CLI's counters are cumulative.
    session = next(iter(client.app.state.sessions._sessions))
    assert session._usage_totals["input_tokens"] == usage["prompt_tokens"] + next_usage["prompt_tokens"]


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("change", ["retry", "fork", "system", "tools", "model"])
def test_changed_history_or_configuration_reseeds_cleanly(client, change, automatic):
    auth = {"Authorization": "Bearer owner-a"} if automatic else headers()
    initial = [{"role": "user", "content": "INSPECT_CONTEXT original"}]
    reply, first, _ = inspect(client, initial, headers=auth)
    history = initial + [reply, {"role": "user", "content": "INSPECT_CONTEXT continuation"}]
    body = {}
    if change == "retry":
        history = initial
    elif change == "fork":
        history[0] = {"role": "user", "content": "edited first message"}
    elif change == "system":
        history.insert(0, {"role": "system", "content": "new rules"})
    elif change == "tools":
        body = {"parallel_tool_calls": False}
    else:
        body = {"model": "gemini-3-pro-high"}
    _, second, _ = inspect(client, history, body=body, headers=auth)
    assert second["pid"] != first["pid"] and second["turn"] == 1
    assert second["history"] == history


def test_session_id_is_credential_scoped_and_clone_sessions_stay_separate(client):
    history = [{"role": "user", "content": "INSPECT_CONTEXT same input"}]
    states = [inspect(client, history, headers=headers(s, o))[1]
              for s, o in [("same", "owner-a"), ("same", "owner-b"), ("fork", "owner-a")]]
    assert len({s["pid"] for s in states}) == 3


@pytest.mark.parametrize("automatic", [False, True])
def test_client_tool_result_reuses_session_across_stream_modes(client, automatic):
    auth = {"Authorization": "Bearer owner-a"} if automatic else headers()
    tool = {"type": "function", "function": {"name": "Read", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
    history = [{"role": "user", "content": "read the client file"}]
    body = {"model": "auto", "tools": [tool], "messages": history}
    first = client.post("/v1/chat/completions", json=body, headers=auth).json()
    reply = first["choices"][0]["message"]
    session = next(iter(client.app.state.sessions._sessions))
    pid = session.proc.pid
    body["messages"] = history + [reply, {"role": "tool", "tool_call_id": reply["tool_calls"][0]["id"],
                                         "content": "real client file contents"}]
    body.update(stream=True, stream_options={"include_usage": True})
    second = client.post("/v1/chat/completions", json=body, headers=auth)
    assert second.status_code == 200 and '"finish_reason":"stop"' in second.text
    assert "real client file contents" in second.text
    assert session.proc.pid == pid and session.turns == 2 and not session.busy
    assert client.app.state.sessions.live_count == 1


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("protocol", ["gemini", "responses", "messages", "structured", "plain"])
def test_session_affinity_across_api_adapters(client, protocol, automatic):
    auth = {"Authorization": "Bearer owner-a"} if automatic else headers()
    if protocol == "gemini":
        url = "/v1beta/models/auto:generateContent"
        body = {"contents": [{"role": "user", "parts": [{"text": "PING"}]}]}
    elif protocol == "responses":
        url = "/v1/responses"
        body = {"model": "auto", "input": "hello"}
    elif protocol == "messages":
        url = "/v1/messages"
        body = {"model": "auto", "max_tokens": 100,
                "messages": [{"role": "user", "content": "hello"}]}
    else:
        url = "/v1/chat/completions"
        body = {"model": "auto", "messages": [{"role": "user", "content": "first person"}]}
        if protocol == "structured":
            body["response_format"] = {"type": "json_object"}
    first = client.post(url, json=body, headers=auth)
    assert first.status_code == 200, first.text
    result = first.json()
    session = next(iter(client.app.state.sessions._sessions))
    if protocol == "gemini":
        body["contents"] += [result["candidates"][0]["content"],
                             {"role": "user", "parts": [{"text": "next"}]}]
    elif protocol == "responses":
        body.update(previous_response_id=result["id"], input="next")
    elif protocol == "messages":
        body["messages"] += [{"role": "assistant", "content": result["content"]}, {"role": "user", "content": "next"}]
    else:
        body["messages"] += [result["choices"][0]["message"], {"role": "user", "content": "next person"}]
    second = client.post(url, json=body, headers=auth)
    assert second.status_code == 200, second.text
    assert session.alive and session.turns == 2 and not session.busy
    assert client.app.state.sessions.live_count == 1


def test_cache_key_is_not_a_session_identity_without_opt_in(client):
    initial = [{"role": "user", "content": "INSPECT_CONTEXT first"}]
    auth = {"Authorization": "Bearer owner-a"}
    _, first, _ = inspect(client, initial, headers=auth, body={"prompt_cache_key": "shared-cache"})
    _, other, _ = inspect(client, initial, headers=auth, body={"prompt_cache_key": "shared-cache"})
    assert first["pid"] != other["pid"]
    affinity = {**auth, "X-Agy-Session-Source": "prompt-cache-key"}
    reply, first, _ = inspect(client, initial, headers=affinity, body={"prompt_cache_key": "session-a"})
    _, second, _ = inspect(client, initial + [reply, {"role": "user", "content": "INSPECT_CONTEXT next"}],
                           headers=affinity, body={"prompt_cache_key": "session-a"})
    assert first["pid"] == second["pid"]
    bad = client.post("/v1/chat/completions", json={"model": "auto", "messages": initial}, headers=affinity)
    assert bad.status_code == 400


def test_server_recognizes_three_turns_without_any_session_headers(client):
    auth = {"Authorization": "Bearer owner-a"}
    history = [{"role": "user", "content": "INSPECT_CONTEXT first"}]
    identities = []
    for turn in range(1, 4):
        reply, state, _ = inspect(client, history, headers=auth)
        session = next(iter(client.app.state.sessions._sessions))
        identities.append((state["pid"], session.conversation_id, session.client_context.key))
        assert state["turn"] == turn and state["history"] == history
        assert session.client_context.automatic and session.client_context.owner
        history += [reply, {"role": "user", "content": f"INSPECT_CONTEXT next {turn}"}]
    assert len(set(identities)) == 1
    assert client.app.state.sessions.live_count == 1


def test_automatic_history_cannot_cross_credentials_or_explicit_sessions(client):
    history = [{"role": "user", "content": "INSPECT_CONTEXT original"}]
    reply, first, _ = inspect(client, history, headers={"Authorization": "Bearer owner-a"})
    follow = history + [reply, {"role": "user", "content": "INSPECT_CONTEXT next"}]
    _, other, _ = inspect(client, follow, headers={"Authorization": "Bearer owner-b"})
    _, explicit, _ = inspect(client, follow, headers=headers())
    _, resumed, _ = inspect(client, follow, headers={"Authorization": "Bearer owner-a"})
    assert first["pid"] == resumed["pid"]
    assert len({first["pid"], other["pid"], explicit["pid"]}) == 3
    assert other["turn"] == explicit["turn"] == 1 and resumed["turn"] == 2


def test_ambiguous_identical_histories_do_not_choose_an_arbitrary_process(client):
    auth = {"Authorization": "Bearer owner-a"}
    initial = [{"role": "user", "content": "same first input"}]
    body = {"model": "auto", "tools": [], "messages": initial}
    replies = [client.post("/v1/chat/completions", json=body, headers=auth).json()["choices"][0]["message"]
               for _ in range(2)]
    assert replies[0] == replies[1]
    old_pids = {s.proc.pid for s in client.app.state.sessions._sessions}
    assert len(old_pids) == 2
    _, next_state, _ = inspect(client, initial + [replies[0], {"role": "user", "content": "INSPECT_CONTEXT next"}],
                                headers=auth)
    assert next_state["pid"] not in old_pids and next_state["turn"] == 1


def test_automatic_busy_continuation_rejects_duplicate_but_isolates_branch():
    async def check():
        mgr = SessionManager(config(max_sessions=2))
        identity = SessionIdentity("owner")
        initial = [("user", "PING")]
        try:
            first = await mgr.acquire(model="auto", effort=None,
                                      context=SessionContext.build(identity, {}, initial))
            async with aclosing(first.send_turn("PING")) as events:
                result = [e async for e in events][-1]["result"]
            reply = ("assistant", result["response"])
            await mgr.release(first, reply=reply)
            context = SessionContext.build(identity, {}, initial + [reply, ("user", "left")])
            resumed = await mgr.acquire(model="auto", effort=None, context=context)
            assert resumed is first
            with pytest.raises(AgyError) as caught:
                await mgr.acquire(model="auto", effort=None, context=context)
            assert caught.value.status_code == 409
            branch = await mgr.acquire(model="auto", effort=None,
                                      context=SessionContext.build(identity, {}, initial + [reply, ("user", "right")]))
            assert branch is not first and branch.input_offset == 0
            assert mgr.busy_count() == 2
        finally:
            await mgr.close_all()
    asyncio.run(check())


@pytest.mark.parametrize("convention", ["generic-header", "opencode", "metadata", "claude-code", "codex"])
def test_agent_identity_conventions_are_not_specific_to_dsh(client, convention):
    auth = {"Authorization": "Bearer owner-a"}
    body = {}
    if convention == "generic-header":
        auth["X-Agy-Session-Id"] = "agent-chat"
    elif convention == "opencode":
        auth["X-Opencode-Session"] = "agent-chat"
    elif convention == "metadata":
        body["metadata"] = {"session_id": "agent-chat"}
    elif convention == "claude-code":
        body["metadata"] = {"user_id": json.dumps({"device_id": "device", "account_uuid": "account",
                                                  "session_id": "agent-chat"})}
    else:
        body["client_metadata"] = {"thread_id": "agent-chat", "session_id": "agent-chat", "turn_id": "turn-1"}
    history = [{"role": "user", "content": "INSPECT_CONTEXT first"}]
    reply, first, _ = inspect(client, history, headers=auth, body=body)
    if convention == "codex":
        body["client_metadata"]["turn_id"] = "turn-2"
    _, second, _ = inspect(client, history + [reply, {"role": "user", "content": "INSPECT_CONTEXT next"}],
                           headers=auth, body=body)
    assert first["pid"] == second["pid"] and second["turn"] == 2


def test_concurrent_turns_in_same_session_are_rejected_and_cancel_drops_state():
    async def check():
        mgr = SessionManager(config(max_sessions=1))
        context = SessionContext.build("owner-session", {}, [("user", "hello")])
        try:
            first = await mgr.acquire(model="auto", effort=None, context=context)
            with pytest.raises(AgyError) as exc:
                await mgr.acquire(model="auto", effort=None, context=context)
            assert exc.value.status_code == 409
            await mgr.release(first)  # canceled before a validated result
            assert not first.alive and mgr.live_count == 0
            second = await mgr.acquire(model="auto", effort=None, context=context)
            assert second.proc.pid != first.proc.pid
        finally:
            await mgr.close_all()
    asyncio.run(check())


def test_idle_ttl_reaps_session_gracefully():
    async def check():
        cfg = config(session_ttl=0.01)
        mgr = SessionManager(cfg)
        try:
            async for _ in run_turn(cfg, mgr, system="", history=[], final_user="PING", model="auto",
                                    effort=None, session_key="session"):
                pass
            session = next(iter(mgr._sessions))
            await asyncio.sleep(0.02)
            await mgr.sweep()
            assert mgr.live_count == 0 and session.proc.returncode == 0
        finally:
            await mgr.close_all()
    asyncio.run(check())


@pytest.mark.parametrize("model,expected", [("bad-ndjson-model", "NDJSON"), ("exit-zero-model", "code 0"),
    ("exit-two-model", "code 2"), ("status-ERROR", "terminal failure"), ("status-CANCELED", "terminal failure"),
    ("status-INTERRUPTED", "terminal failure"), ("status-INVALID", "terminal failure"),
    ("status-WAITING", "terminal failure"), ("status-RUNNING", "terminal failure")])
def test_malformed_stdout_and_terminal_failures_are_not_success(model, expected):
    async def check():
        cfg = config(turn_timeout=3)
        mgr = SessionManager(cfg)
        try:
            async with aclosing(run_turn(cfg, mgr, system="", history=[], final_user="PING", model=model,
                                         effort=None, session_key="session")) as turn:
                with pytest.raises(AgyError, match=expected):
                    async for _ in turn:
                        pass
            assert mgr.live_count == 0
        finally:
            await mgr.close_all()
    asyncio.run(check())


@pytest.mark.parametrize("model,expected", [("tail-model", "哈哈\n"), ("unknown-event-model", "pong")])
def test_documented_deltas_and_unknown_events(model, expected):
    async def check():
        cfg = config()
        mgr = SessionManager(cfg)
        try:
            values = [e async for e in run_turn(cfg, mgr, system="", history=[], final_user="PING",
                                              model=model, effort=None)]
            assert "".join(v for k, v in values if k == "delta") == expected
            assert values[-1][1].text == expected
        finally:
            await mgr.close_all()
    asyncio.run(check())


def test_silent_stdout_uses_total_deadline_not_the_old_idle_default():
    async def check():
        cfg = config(turn_timeout=0.3)
        assert cfg.idle_timeout == 0
        mgr = SessionManager(cfg)
        try:
            with pytest.raises(AgyError, match="turn timed out"):
                async for _ in run_turn(cfg, mgr, system="", history=[], final_user="PING",
                                        model="slow-model", effort=None):
                    pass
            assert mgr.live_count == 0
            cfg.turn_timeout = 4
            results = [v async for k, v in run_turn(cfg, mgr, system="", history=[], final_user="PING",
                                                   model="slow-model", effort=None) if k == "result"]
            assert results[0].text == "pong"
        finally:
            await mgr.close_all()
    asyncio.run(check())


def test_stream_starts_and_heartbeats_before_validated_tool_result(tmp_path):
    async def check():
        cfg = config(tmp_path, tools=True, stream_heartbeat=0.04)
        app = create_app(cfg)
        body = {"model": "auto", "tools": [], "stream": True,
                "messages": [{"role": "user", "content": "SLOW_OUTPUT"}]}
        async def receive():
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                           "headers": [], "app": app, "query_string": b""}, receive=receive)
        try:
            response = await chat_completions(request)
            events = response.body_iterator
            assert b'"role":"assistant"' in await anext(events)
            start = time.monotonic()
            assert await asyncio.wait_for(anext(events), 0.4) == b": keep-alive\n\n"
            assert time.monotonic() - start < 0.4
            chunks = [chunk async for chunk in events]
            assert b"data: [DONE]" in b"".join(chunks)
            assert b'"finish_reason":"stop"' in b"".join(chunks)
            assert app.state.sessions.live_count == 1
            assert app.state.sessions.busy_count() == 0
        finally:
            await app.state.sessions.close_all()
    asyncio.run(check())


@pytest.mark.parametrize("abort", ["before_headers", "during_heartbeat"])
def test_disconnect_reaps_preflight_or_pending_generation(tmp_path, abort):
    from starlette.requests import ClientDisconnect
    async def check():
        app = create_app(config(tmp_path, tools=True, stream_heartbeat=0.03))
        body = {"model": "auto", "tools": [], "stream": True,
                "messages": [{"role": "user", "content": "SLOW_OUTPUT"}]}
        async def receive():
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
                 "headers": [(b'x-agy-session-id', b'cancelled-agent')], "app": app,
                 "query_string": b"", "asgi": {"spec_version": "2.4"}}
        try:
            response = await chat_completions(Request(scope, receive=receive))
            assert app.state.sessions.busy_count() == 1
            if abort == "before_headers":
                async def send(_message):
                    raise OSError("connection closed before headers")
                with pytest.raises(ClientDisconnect):
                    await response(scope, receive, send)
            else:
                await anext(response.body_iterator)
                assert await anext(response.body_iterator) == b": keep-alive\n\n"
                await response.body_iterator.aclose()
            assert app.state.sessions.live_count == 0
        finally:
            await app.state.sessions.close_all()
    asyncio.run(check())


def test_orjson_rejects_nonfinite_and_invalid_utf8_at_boundaries(client):
    for content in [b'{"model":"auto","value":NaN}', b'{"model":"\xff"}']:
        response = client.post("/v1/chat/completions", content=content, headers=headers())
        assert response.status_code == 400
    from app.json_codec import dumps_text
    with pytest.raises(ValueError):
        dumps_text({"value": float("nan")}, strict=True)


def test_reasoning_tokens_are_part_of_output_not_added_twice():
    from app.anthropic_api import message_result
    from app.gemini_api import _usage_meta
    from app.openai_api import _usage
    from app.runner import TurnResult
    usage = {"input_tokens": 100, "output_tokens": 27, "thinking_tokens": 26,
             "cache_read_tokens": 50, "total_tokens": 127}
    assert _usage(usage)["completion_tokens_details"]["reasoning_tokens"] == 26
    assert _usage(usage)["total_tokens"] == 127
    assert _usage_meta(usage)["candidatesTokenCount"] == 1
    anthropic = message_result(TurnResult(text="apple", usage=usage), "auto", "msg", [])
    assert anthropic["usage"]["output_tokens"] == 27
    assert anthropic["usage"]["cache_read_input_tokens"] == 50
