"""Four wire protocols, real fake-CLI processes, and shared recovery invariants."""
import asyncio
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.agy import AgyError, SessionManager
from app.config import Config
from app.contracts import internal_name
from app.external_tools import ExternalTools
from app.runner import TurnProfile
from app.session_context import SessionIdentity

PROTOCOLS = ["chat", "responses", "messages", "gemini"]
SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}},
          "required": ["path"], "additionalProperties": False}


@pytest.fixture
def client(tmp_path, monkeypatch):
    trace = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_AGY_ARGV_FILE", str(trace))
    turns = tmp_path / "turns.jsonl"
    monkeypatch.setenv("FAKE_AGY_TURNS_FILE", str(turns))
    cfg = Config(agy_bin=f'{sys.executable} {Path(__file__).with_name("fake_tools_agy.py").as_posix()}',
                 data_dir=str(tmp_path / "data"), stream_heartbeat=0.03)
    with TestClient(create_app(cfg)) as client:
        client.trace = trace
        client.turns_trace = turns
        yield client


def wire(protocol, text="READ", *, schema=SCHEMA, name="bash", choice=None, stream=False, tools=True):
    if protocol == "chat":
        body = {"model": "auto", "messages": [{"role": "user", "content": text}], "stream": stream}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": name, "parameters": schema}}]
        if choice:
            body["tool_choice"] = {"type": "function", "function": {"name": name}}
        return "/v1/chat/completions", body
    if protocol == "responses":
        body = {"model": "auto", "input": [{"role": "user", "content": text}], "stream": stream}
        if tools:
            body["tools"] = [{"type": "function", "name": name, "parameters": schema}]
        if choice:
            body["tool_choice"] = {"type": "function", "name": name}
        return "/v1/responses", body
    if protocol == "messages":
        body = {"model": "auto", "max_tokens": 2000, "messages": [{"role": "user", "content": text}], "stream": stream}
        if tools:
            body["tools"] = [{"name": name, "input_schema": schema}]
        if choice:
            body["tool_choice"] = {"type": "tool", "name": name}
        return "/v1/messages", body
    body = {"contents": [{"role": "user", "parts": [{"text": text}]}]}
    if tools:
        body["tools"] = [{"functionDeclarations": [{"name": name, "parameters": schema}]}]
    if choice:
        body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}
    return "/v1beta/models/auto:" + ("streamGenerateContent" if stream else "generateContent"), body


def payload(response, protocol, stream):
    assert response.status_code == 200, response.text
    assert response.headers["x-agy-request-id"].startswith("agy_")
    if not stream:
        return response.json()
    events = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    assert not any("error" in e and e["error"] for e in events), response.text
    if protocol == "responses":
        return events[-1]["response"]
    if protocol == "messages":
        blocks = []
        for e in events:
            if e["type"] == "content_block_start":
                blocks.append(e["content_block"])
            if e["type"] == "content_block_delta":
                delta = e["delta"]
                if delta["type"] == "input_json_delta":
                    blocks[-1]["input"] = json.loads(delta["partial_json"])
                else:
                    blocks[-1]["text"] = delta["text"]
        assert events[-1]["type"] == "message_stop"
        return {"role": "assistant", "content": blocks}
    if protocol == "chat":
        message = {"role": "assistant", "content": ""}
        for e in events:
            if e.get("choices"):
                delta = e["choices"][0]["delta"]
                message["content"] += delta.get("content", "")
                if delta.get("tool_calls"):
                    message["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in delta["tool_calls"]]
        return {"choices": [{"message": message}]}
    return events[-1]


def calls_from(protocol, result):
    if protocol == "chat":
        return [(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"]))
                for c in result["choices"][0]["message"].get("tool_calls", [])]
    if protocol == "responses":
        return [(c["call_id"], c["name"], json.loads(c["arguments"])) for c in result["output"] if c["type"] == "function_call"]
    if protocol == "messages":
        return [(c["id"], c["name"], c["input"]) for c in result["content"] if c["type"] == "tool_use"]
    return [(p["functionCall"]["id"], p["functionCall"]["name"], p["functionCall"]["args"])
            for p in result["candidates"][0]["content"]["parts"] if "functionCall" in p]


def append_results(protocol, body, result, calls, output="CLIENT_RESULT"):
    if protocol == "chat":
        body["messages"] += [result["choices"][0]["message"]] + [
            {"role": "tool", "tool_call_id": cid, "content": output} for cid, _, _ in calls]
        body.pop("tool_choice", None)
    elif protocol == "responses":
        body["input"] += result["output"] + [
            {"type": "function_call_output", "call_id": cid, "output": output} for cid, _, _ in calls]
        body.pop("tool_choice", None)
    elif protocol == "messages":
        body["messages"] += [{"role": "assistant", "content": result["content"]}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": cid, "content": output} for cid, _, _ in calls]}]
        body.pop("tool_choice", None)
    else:
        body["contents"] += [result["candidates"][0]["content"], {"role": "user", "parts": [
            {"functionResponse": {"id": cid, "name": name, "response": {"output": output}}} for cid, name, _ in calls]}]
        body.pop("toolConfig", None)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["plain", "tool_result"])
def test_long_context_reaches_cli_without_truncation(client, protocol, stream, mode):
    text = "BEGIN_LONG_CONTEXT_" + "x" * 210_000 + "_END_LONG_CONTEXT"
    url, body = wire(protocol, text if mode == "plain" else "READ",
                     stream=stream, tools=mode == "tool_result")
    if mode == "tool_result":
        first = payload(client.post(url, json=body), protocol, stream)
        calls = calls_from(protocol, first)
        assert len(calls) == 1
        append_results(protocol, body, first, calls, output=text)
    result = payload(client.post(url, json=body), protocol, stream)
    assert calls_from(protocol, result) == []
    # The fake CLI echoes the received content, proving it was delivered intact.
    assert text in json.dumps(result)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_long_tool_definition_reaches_cli(client, protocol):
    url, body = wire(protocol)
    definition = (body["tools"][0]["function"] if protocol == "chat" else
                  body["tools"][0]["functionDeclarations"][0] if protocol == "gemini" else
                  body["tools"][0])
    definition["description"] = "Tool reference: " + "x" * 210_000
    result = payload(client.post(url, json=body), protocol, False)
    assert len(calls_from(protocol, result)) == 1


def test_count_tokens_accepts_long_context(client):
    _, body = wire("messages", "x" * 210_000, tools=False)
    response = client.post("/v1/messages/count_tokens", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["input_tokens"] > 0
    assert response.headers["x-agy-token-count"] == "estimate"


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["plain", "single", "named", "parallel"])
def test_four_protocol_roundtrips(client, protocol, stream, mode, caplog):
    caplog.set_level("INFO", logger="agy_proxy.execution")
    url, body = wire(protocol, "MULTI" if mode == "parallel" else "READ", stream=stream,
                     choice=mode == "named", tools=mode != "plain")
    result = payload(client.post(url, json=body), protocol, stream)
    calls = calls_from(protocol, result)
    assert len(calls) == (0 if mode == "plain" else 2 if mode == "parallel" else 1)
    if calls:
        append_results(protocol, body, result, calls)
        final = payload(client.post(url, json=body), protocol, stream)
        assert calls_from(protocol, final) == []
        assert "CLIENT_RESULT" in json.dumps(final)
    argv = [json.loads(line)["argv"] for line in client.trace.read_text().splitlines()]
    assert all("--agent" in args and "--json-schema" in args for args in argv)
    assert all(s.output_schema is not None for s in client.app.state.sessions._sessions)
    assert "outcome=success error_code=cancelled" not in caplog.text


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
def test_tool_configuration_lives_once_in_session_not_user_history(client, protocol, stream):
    url, body = wire(protocol, stream=stream)
    first = payload(client.post(url, json=body), protocol, stream)
    append_results(protocol, body, first, calls_from(protocol, first), output="PRESERVED_CLIENT_RESULT")
    final = payload(client.post(url, json=body), protocol, stream)
    assert "PRESERVED_CLIENT_RESULT" in json.dumps(final)
    turns = [json.loads(line) for line in client.turns_trace.read_text().splitlines()]
    assert len(turns) == 2 and turns[0]["pid"] == turns[1]["pid"]
    assert [t["turn"] for t in turns] == [1, 2]
    agent = turns[0]["agent"]
    assert agent.count("[Client tool configuration]\n") == 1
    config = json.JSONDecoder().raw_decode(agent.split("[Client tool configuration]\n")[1])[0]
    assert config["tools"][0]["parameters"] == SCHEMA
    assert turns[1]["agent"] is None  # no session re-registration on continuation
    assert all("[Client tool configuration]" not in t["prompt"] for t in turns)
    assert all("[Bridge output contract]" not in t["prompt"] for t in turns)

    # A contract change creates a new session without adding declarations to
    # its replayed client history, including historical tool arguments/results.
    definition = (body["tools"][0]["function"] if protocol == "chat" else
                  body["tools"][0]["functionDeclarations"][0] if protocol == "gemini" else
                  body["tools"][0])
    definition["description"] = "UPDATED_SESSION_TOOL_DESCRIPTION"
    replay = payload(client.post(url, json=body), protocol, stream)
    assert "PRESERVED_CLIENT_RESULT" in json.dumps(replay)
    changed = json.loads(client.turns_trace.read_text().splitlines()[-1])
    assert changed["pid"] != turns[0]["pid"] and changed["turn"] == 1
    assert changed["agent"].count("[Client tool configuration]\n") == 1
    assert "UPDATED_SESSION_TOOL_DESCRIPTION" in changed["agent"]
    assert "UPDATED_SESSION_TOOL_DESCRIPTION" not in changed["prompt"]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("arguments", [{"command": "synthetic", "description": "fixture"}, {"cmd": "synthetic"}])
def test_repair_preserves_each_clients_bash_contract(client, protocol, stream, arguments):
    schema = {"type": "object", "properties": {k: {"type": "string"} for k in arguments},
              "required": list(arguments), "additionalProperties": False}
    url, body = wire(protocol, "REPAIR_ARGS\n" + json.dumps(arguments), schema=schema, stream=stream)
    response = client.post(url, json=body)
    result = payload(response, protocol, stream)
    assert calls_from(protocol, result)[0][2] == arguments
    records = list(client.app.state.store.requests)
    assert len(records) == 1
    diagnostic = records[0].public()
    assert diagnostic["attempts"] == 2 and diagnostic["repair_attempts"] == 1
    assert diagnostic["usage_complete"] is True and diagnostic["usage"]["total_tokens"] > 0
    assert diagnostic["request_id"] == response.headers["x-agy-request-id"]
    assert client.app.state.sessions.live_count == 0
    turns = [json.loads(line) for line in client.turns_trace.read_text().splitlines()]
    assert len(turns) == 2 and turns[0]["pid"] != turns[1]["pid"]
    assert turns[0]["agent"] == turns[1]["agent"]
    assert turns[0]["agent"].count("[Client tool configuration]\n") == 1
    assert all("[Client tool configuration]" not in t["prompt"] for t in turns)
    assert "[Validation feedback]" in turns[1]["prompt"]
    assert "[Validation feedback]" not in turns[1]["agent"]
    # Recovery never becomes part of cached/confirmed history.
    append_results(protocol, body, result, calls_from(protocol, result))
    final = payload(client.post(url, json=body), protocol, stream)
    assert "Rejected candidate" not in json.dumps(final)
    assert all(s.turns == 1 for s in client.app.state.sessions._sessions)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
def test_failed_candidates_never_leak_or_emit_success(client, protocol, stream):
    url, body = wire(protocol, "BAD_ARGS", stream=stream)
    response = client.post(url, json=body)
    assert response.status_code == (200 if stream else 502)
    assert '"path":42' not in response.text and '"path": 42' not in response.text
    assert "tool_arguments_invalid" in response.text
    if stream:
        assert all(marker not in response.text for marker in ('"finish_reason":"tool_calls"', '"type":"response.completed"',
                                                              '"type":"message_stop"', '"finishReason":"STOP"', 'data: [DONE]'))
    records = list(client.app.state.store.requests)
    assert len(records) == 1
    record = records[0].public()
    assert record["attempts"] == 2 and record["repair_attempts"] == 1
    assert record["error_code"] == "tool_arguments_invalid"
    assert record["usage"]["total_tokens"] > 0 and record["usage_complete"]
    assert not client.app.state.sessions._request_leases


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_recovery_off_and_native_tools_never_retry(client, protocol):
    client.app.state.cfg.recovery_attempts = 0
    url, body = wire(protocol, "BAD_ARGS")
    assert client.post(url, json=body).status_code == 502
    assert client.app.state.store.requests[-1].public()["attempts"] == 1
    client.app.state.cfg.recovery_attempts = 1
    url, body = wire(protocol, "NATIVE_TOOL", tools=False)
    response = client.post(url, json=body)
    assert response.status_code == 502 and "native_tool_violation" in response.text
    assert client.app.state.store.requests[-1].public()["attempts"] == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_long_namespace_name_and_schema_data_keys_preserved(client, protocol):
    name = "mcp.vendor/" + "very_long_name" * 9
    args = {"title": "keep", "description": "keep too", "default": {"$ref": "literal data"}}
    schema = {"type": "object", "$defs": {"text": {"type": "string"}}, "properties": {
        "title": {"$ref": "#/$defs/text"}, "description": {"allOf": [{"type": "string"}, {"enum": ["keep too"]}]},
        "default": {"const": {"$ref": "literal data"}}}, "required": list(args), "additionalProperties": False}
    url, body = wire(protocol, "ECHO_ARGUMENTS\n" + json.dumps(args), name=name, schema=schema, choice=True)
    result = payload(client.post(url, json=body), protocol, False)
    assert calls_from(protocol, result)[0][1:] == (name, args)
    append_results(protocol, body, result, calls_from(protocol, result))
    # A new schema must never revalidate old arguments.
    if protocol == "chat":
        body["tools"][0]["function"]["parameters"] = {"type": "object", "required": ["new_required"]}
    final = payload(client.post(url, json=body), protocol, False)
    assert "CLIENT_RESULT" in json.dumps(final)


def test_alias_collision_rejected_before_cli(client):
    name = "vendor.namespace/bash"
    url, body = wire("chat", name=name)
    body["tools"].append({"type": "function", "function": {"name": internal_name(name)}})
    assert client.post(url, json=body).status_code == 400
    assert not client.trace.exists()


def test_gemini_nullable_and_no_id_matching(client):
    url, body = wire("gemini", "ECHO_ARGUMENTS\n" + json.dumps({"x": None}), schema={
        "type": "OBJECT", "properties": {"x": {"type": "STRING", "nullable": True}}, "required": ["x"]})
    result = payload(client.post(url, json=body), "gemini", False)
    append_results("gemini", body, result, calls_from("gemini", result))
    for content in body["contents"]:
        for part in content["parts"]:
            for key in ("functionCall", "functionResponse"):
                if key in part:
                    part[key].pop("id", None)
    assert client.post(url, json=body).status_code == 200
    call_content = body["contents"][1]
    call_content["parts"].append(deepcopy(call_content["parts"][0]))
    ambiguous = client.post(url, json=body)
    assert ambiguous.status_code == 400 and "uniquely" in ambiguous.text


def test_repair_deadline_and_session_lease(client, monkeypatch):
    client.app.state.cfg.turn_timeout = 0.8
    url, body = wire("chat", "SLOW_REPAIR SLOW_OUTPUT")
    start = time.monotonic()
    response = client.post(url, json=body)
    assert response.status_code == 504
    assert time.monotonic() - start < 1.5
    assert client.app.state.store.requests[-1].public()["attempts"] == 2
    assert client.app.state.sessions.live_count == 0
    assert not client.app.state.sessions._request_leases


def test_cancel_during_rebuild_holds_lease_and_releases_resources(tmp_path):
    async def scenario():
        cfg = Config(agy_bin=f'{sys.executable} {Path(__file__).with_name("fake_tools_agy.py").as_posix()}')
        mgr = SessionManager(cfg)
        entered, resume = asyncio.Event(), asyncio.Event()
        restart = mgr.restart

        async def blocked(session, **kw):
            entered.set()
            await resume.wait()
            return await restart(session, **kw)
        mgr.restart = blocked
        _, body = wire("chat", "REPAIR_ARGS\n{\"path\":\"client.txt\"}")
        bridge = ExternalTools.from_body(body)
        key = SessionIdentity("owner", "same-session")

        async def consume():
            async for _ in bridge.run(mgr, model="auto", effort=None, profile=TurnProfile(), session_key=key):
                pass
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(AgyError) as error:
                await consume()
            assert error.value.status_code == 409
            assert mgr.busy_count() == 1
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert mgr.live_count == 0 and not mgr._request_leases
        finally:
            resume.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await mgr.close_all()
    asyncio.run(scenario())


@pytest.mark.parametrize("first", ["quota", "transport", "arguments"])
def test_one_budget_covers_quota_transport_and_argument_repair(client, monkeypatch, first):
    from app.agy import AgySession, AgyQuotaError
    pool = client.app.state.cfg.account_pool
    credentials = {"token": {"access_token": "SYNTHETIC", "refresh_token": "SYNTHETIC",
                              "expiry": "2099-01-01T00:00:00Z"}, "auth_method": "consumer"}
    pool.create("A", credentials)
    credentials = deepcopy(credentials)
    credentials["token"]["refresh_token"] = "DISTINCT_SYNTHETIC_B"
    pool.create("B", credentials)
    calls = []
    original = AgySession.send_turn

    async def scripted(self, prompt):
        calls.append((self.account_id, self.proc.pid, prompt))
        if len(calls) == 1 and first != "arguments":
            error = (AgyQuotaError("weekly quota exhausted", self.account_id, self.model, retry_safe=True)
                     if first == "quota" else AgyError("Transient failure", code="cli_transport_error", retryable=True))
            error.usage = {"input_tokens": 13, "output_tokens": 2, "total_tokens": 15}
            raise error
        if len(calls) == 2 and first == "arguments":
            raise AgyQuotaError("weekly quota exhausted", self.account_id, self.model, retry_safe=True)
        async for event in original(self, prompt):
            yield event
    monkeypatch.setattr(AgySession, "send_turn", scripted)
    url, body = wire("chat", "BAD_ARGS")
    response = client.post(url, json=body)
    assert response.status_code in (429, 502)
    assert len(calls) == 2 and calls[0][1] != calls[1][1]
    assert (calls[0][0] != calls[1][0]) == (first == "quota")
    record = client.app.state.store.requests[-1].public()
    assert record["attempts"] == 2
    assert record["repair_attempts"] == (1 if first == "arguments" else 0)
    assert record["usage"]["total_tokens"] > 15


def test_nonstream_disconnect_cancels_generation_and_records_once(tmp_path):
    from starlette.requests import Request
    from app.openai_api import chat_completions

    async def scenario():
        cfg = Config(agy_bin=f'{sys.executable} {Path(__file__).with_name("fake_tools_agy.py").as_posix()}',
                     data_dir=str(tmp_path))
        app = create_app(cfg)
        _, body = wire("chat", "SLOW_OUTPUT")
        pending = asyncio.Queue()
        await pending.put({"type": "http.request", "body": json.dumps(body).encode(), "more_body": False})
        request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": [],
                           "app": app, "query_string": b""}, receive=pending.get)
        task = asyncio.create_task(chat_completions(request))
        try:
            async with asyncio.timeout(5):
                while not app.state.sessions.busy_count():
                    await asyncio.sleep(0.01)
            await pending.put({"type": "http.disconnect"})
            result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            assert isinstance(result[0], asyncio.CancelledError)
            assert app.state.sessions.live_count == 0 and not app.state.sessions._request_leases
            assert len(app.state.store.requests) == 1
            assert app.state.store.requests[-1].status == "ABORTED"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await app.state.sessions.close_all()
    asyncio.run(scenario())


def test_schema_dialect_is_enforced_and_unknown_dialects_rejected(client):
    schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
              "dependencies": {"path": ["description"]}}
    url, body = wire("chat", schema=schema)
    response = client.post(url, json=body)
    assert response.status_code == 502 and "dependencies" in response.text
    body["tools"][0]["function"]["parameters"]["$schema"] = "https://example.invalid/dialect"
    assert client.post(url, json=body).status_code == 400
