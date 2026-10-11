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
                                         effort=None)) as turn:
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
            assert app.state.sessions.live_count == 0
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
