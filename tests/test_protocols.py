from __future__ import annotations

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.config import Config
from app.responses_api import ResponseCache

SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
RT = {"type": "function", "name": "read", "parameters": SCHEMA}
AT = {"name": "read", "input_schema": SCHEMA}


@pytest.fixture
def client(tmp_path):
    cfg = Config(agy_bin=f'{sys.executable} {(Path(__file__).parent / "fake_tools_agy.py").as_posix()}',
                 data_dir=str(tmp_path / "data"), agy_cwd=str(tmp_path), api_keys=("key-a", "key-b"))
    with TestClient(create_app(cfg)) as c:
        c.headers["Authorization"] = "Bearer key-a"
        yield c


def events(response):
    assert response.status_code == 200, response.text
    result = []
    for block in response.text.split("\n\n"):
        lines = block.splitlines()
        data = [json.loads(line[6:]) for line in lines if line.startswith("data: ")]
        if data:
            assert f'event: {data[0]["type"]}' in lines
            result.extend(data)
    return result


@pytest.mark.parametrize("stream", [False, True])
def test_responses_multiple_tool_roundtrip(client, stream):
    body = {"model": "auto", "input": "MULTI", "tools": [RT], "stream": stream}
    response = client.post("/v1/responses", json=body)
    if stream:
        ev = events(response)
        assert [e["sequence_number"] for e in ev] == list(range(len(ev)))
        assert ev[0]["type"] == "response.created" and ev[-1]["type"] == "response.completed"
        final = ev[-1]["response"]
        assert len([e for e in ev if e["type"] == "response.function_call_arguments.done"]) == 2
    else:
        assert response.status_code == 200, response.text
        final = response.json()
    assert len(final["output"]) == 2
    follow = {"model": "auto", "previous_response_id": final["id"], "input": [
        {"type": "function_call_output", "call_id": call["call_id"], "output": f"RESULT-{i}"}
        for i, call in enumerate(final["output"])], "tools": [RT]}
    response = client.post("/v1/responses", json=follow)
    assert response.status_code == 200, response.text
    text = response.json()["output"][0]["content"][0]["text"]
    assert "RESULT-0" in text and "RESULT-1" in text and "turn=1" in text
    assert client.app.state.sessions.live_count == 0


@pytest.mark.parametrize("stream", [False, True])
def test_messages_multiple_tool_roundtrip(client, stream):
    client.headers.pop("Authorization")
    client.headers["x-api-key"] = "key-a"
    body = {"model": "auto", "messages": [{"role": "user", "content": "MULTI"}],
            "tools": [AT], "max_tokens": 2000, "stream": stream}
    response = client.post("/v1/messages", json=body)
    if stream:
        ev = events(response)
        assert ev[0]["type"] == "message_start" and ev[-1]["type"] == "message_stop"
        assert ev[-2]["delta"]["stop_reason"] == "tool_use"
        blocks = [deepcopy(e["content_block"]) for e in ev if e["type"] == "content_block_start"]
        for e in ev:
            if e["type"] == "content_block_delta":
                blocks[e["index"]]["input"] = json.loads(e["delta"]["partial_json"])
    else:
        assert response.status_code == 200, response.text
        blocks = response.json()["content"]
        assert response.json()["stop_reason"] == "tool_use"
    assert len(blocks) == 2 and blocks[0]["input"] == {"path": "client.txt"}
    body.update(stream=False)
    body["messages"] += [{"role": "assistant", "content": blocks}, {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": block["id"], "content": [{"type": "text", "text": f"RESULT-{i}"}]}
        for i, block in enumerate(blocks)]}]
    final = client.post("/v1/messages", json=body)
    assert final.status_code == 200, final.text
    text = final.json()["content"][0]["text"]
    assert "RESULT-0" in text and "RESULT-1" in text and "turn=1" in text
    assert final.json()["stop_reason"] == "end_turn"
    assert client.app.state.sessions.live_count == 0


def test_custom_namespaced_tool_full_history(client):
    tool = {"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "apply_patch",
            "format": {"type": "grammar", "grammar": {"syntax": "lark", "definition": "start: /.+/"}}}]}
    patch = "*** Begin Patch\n*** Add File: café.txt\n+你好\n*** End Patch"
    body = {"model": "auto", "store": False, "input": "ECHO_ARGUMENTS\n" + json.dumps({"input": patch}),
            "tools": [tool], "stream": True}
    ev = events(client.post("/v1/responses", json=body))
    final = ev[-1]["response"]
    call = final["output"][0]
    assert call["type"] == "custom_tool_call" and call["namespace"] == "functions"
    assert call["name"] == "apply_patch" and call["input"] == patch
    assert any(e["type"] == "response.custom_tool_call_input.done" for e in ev)
    assert client.get("/v1/responses/" + final["id"]).status_code == 404
    body.update(stream=False, input=[{"role": "user", "content": "write file"}, call,
                {"type": "custom_tool_call_output", "call_id": call["call_id"], "output": "PATCH_OK"}])
    response = client.post("/v1/responses", json=body)
    assert response.status_code == 200, response.text
    assert "PATCH_OK" in response.json()["output"][0]["content"][0]["text"]


def test_stored_responses_ownership_delete_and_instructions(client):
    first = client.post("/v1/responses", json={"model": "auto", "input": "hello", "instructions": "OLD_SYSTEM"}).json()
    rid = first["id"]
    assert client.get("/v1/responses/" + rid).json() == first
    other = {"Authorization": "Bearer key-b"}
    assert client.get("/v1/responses/" + rid, headers=other).status_code == 404
    assert client.delete("/v1/responses/" + rid, headers=other).status_code == 404
    assert client.post("/v1/responses", headers=other, json={"model": "auto", "input": "x",
                       "previous_response_id": rid}).status_code == 404
    follow = client.post("/v1/responses", json={"model": "auto", "input": "INSPECT_CONTEXT",
                        "instructions": "NEW_SYSTEM", "previous_response_id": rid}).json()
    history = json.loads(follow["output"][0]["content"][0]["text"])["history"]
    assert [m["content"] for m in history if m["role"] == "system"] == ["NEW_SYSTEM"]
    assert client.delete("/v1/responses/" + rid).json()["deleted"]
    assert client.get("/v1/responses/" + rid).status_code == 404


@pytest.mark.parametrize("endpoint", ["responses", "messages"])
def test_protocol_parallel_same_key_isolation(client, endpoint):
    def run(marker):
        body = {"model": "auto", "input": "INSPECT_CONTEXT:" + marker} if endpoint == "responses" else {
            "model": "auto", "max_tokens": 1000, "messages": [{"role": "user", "content": "INSPECT_CONTEXT:" + marker}]}
        final = client.post("/v1/" + endpoint, json=body).json()
        text = final["output"][0]["content"][0]["text"] if endpoint == "responses" else final["content"][0]["text"]
        return json.loads(text)
    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = list(pool.map(run, ["LEFT", "RIGHT"]))
    assert left["pid"] != right["pid"] and left["cwd"] != right["cwd"]
    assert "RIGHT" not in json.dumps(left["history"]) and "LEFT" not in json.dumps(right["history"])
    assert not Path(left["cwd"]).exists() and not Path(right["cwd"]).exists()


@pytest.mark.parametrize("endpoint, body", [
    ("responses", {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]}),
    ("responses", {"input": [{"type": "function_call_output", "call_id": "missing", "output": "x"}]}),
    ("responses", {"input": "x", "reasoning": "high"}),
    ("responses", {"input": "x", "stream": "true"}),
    ("responses", {"input": "x", "tools": [{"type": "web_search"}]}),
    ("responses", {"input": "x", "background": True}),
    ("messages", {"messages": [{"role": "user", "content": [{"type": "image"}]}]}),
    ("messages", {"messages": [{"role": "user", "content": "x"}], "max_tokens": 0}),
    ("messages", {"messages": [{"role": "unknown", "content": "x"}]}),
    ("messages", {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "missing"}]}]}),
])
def test_validation_before_process(client, endpoint, body):
    response = client.post("/v1/" + endpoint, json={"model": "auto", "max_tokens": 1000, **body})
    assert response.status_code == 400, response.text
    assert "error" in response.json()
    if endpoint == "messages":
        assert response.json()["type"] == "error" and response.headers["request-id"]
    assert client.app.state.sessions.live_count == 0


@pytest.mark.parametrize("endpoint", ["responses", "messages"])
def test_stream_failure_never_completed(client, endpoint):
    body = {"model": "auto", "input": "BAD_ARGS", "tools": [RT], "stream": True} if endpoint == "responses" else {
        "model": "auto", "messages": [{"role": "user", "content": "BAD_ARGS"}], "tools": [AT], "stream": True,
        "max_tokens": 1000}
    ev = events(client.post("/v1/" + endpoint, json=body))
    assert ev[-1]["type"] == ("response.failed" if endpoint == "responses" else "error")
    assert not any(e["type"] in ("response.completed", "message_stop") for e in ev)
    assert client.app.state.sessions.live_count == 0


def test_count_tokens_is_explicit_estimate(client):
    response = client.post("/v1/messages/count_tokens", json={"model": "auto", "messages": [{"role": "user", "content": "你好"}]})
    assert response.status_code == 200
    assert response.json()["input_tokens"] > 0 and response.headers["X-Agy-Token-Count"] == "estimate"
    assert client.app.state.sessions.live_count == 0


def test_cache_capacity_and_expiry():
    cache = ResponseCache(max_entries=1)
    cache.put("a", {"id": "1"}, [])
    cache.put("a", {"id": "2"}, [])
    assert len(cache.entries) == 1 and cache.get("a", "2")
    cache.ttl = -1
    cache.put("a", {"id": "3"}, [])
    cache.prune()
    assert cache.size == 0 and not cache.entries


def test_waiting_cancel_releases_underlying_turn():
    from app.protocol_common import PreparedTurn
    from types import SimpleNamespace
    async def check():
        started, closed = asyncio.Event(), asyncio.Event()
        class Pending:
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
                cfg=SimpleNamespace(stream_heartbeat=10))))
            async def collect(self):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()
        pending = PreparedTurn.waiting(Pending())
        task = asyncio.create_task(anext(pending))
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await pending.aclose()
        assert closed.is_set()
    asyncio.run(check())


@pytest.mark.parametrize("endpoint", ["responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_text_and_json_schema(client, endpoint, stream):
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
              "required": ["name", "age"], "additionalProperties": False}
    if endpoint == "responses":
        body = {"model": "auto", "input": "Return a person", "stream": stream,
                "text": {"format": {"type": "json_schema", "name": "person", "schema": schema}}}
    else:
        body = {"model": "auto", "max_tokens": 1000, "stream": stream,
                "messages": [{"role": "user", "content": "Return a person"}],
                "output_config": {"format": {"type": "json_schema", "schema": schema}}}
    response = client.post("/v1/" + endpoint, json=body)
    assert response.status_code == 200, response.text
    if stream:
        ev = events(response)
        if endpoint == "responses":
            content = ev[-1]["response"]["output"][0]["content"][0]["text"]
            assert any(e["type"] == "response.content_part.done" for e in ev)
        else:
            content = "".join(e["delta"]["text"] for e in ev if e["type"] == "content_block_delta")
    else:
        data = response.json()
        content = data["output"][0]["content"][0]["text"] if endpoint == "responses" else data["content"][0]["text"]
    assert json.loads(content) == {"name": "Budi", "age": 20}


def test_messages_stop_sequence(client):
    response = client.post("/v1/messages", json={"model": "auto", "max_tokens": 1000,
                           "messages": [{"role": "user", "content": "BEFORE_STOP_AFTER"}],
                           "stop_sequences": ["_STOP_"]}).json()
    assert response["stop_reason"] == "stop_sequence" and response["stop_sequence"] == "_STOP_"
    assert response["content"][0]["text"].endswith("BEFORE")


def test_claude_code_trailing_environment_system_message(client):
    result = client.post("/v1/messages", json={"model": "auto", "max_tokens": 1000,
        "system": [{"type": "text", "text": "CLIENT_SYSTEM", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "INSPECT_CONTEXT"},
                     {"role": "system", "content": [{"type": "text", "text": "ENVIRONMENT"}]}]}).json()
    history = json.loads(result["content"][0]["text"])["history"]
    assert [m["role"] for m in history] == ["system", "system", "user"]
    assert history[1]["content"] == "ENVIRONMENT"


@pytest.mark.parametrize("path", ["/v1/responses", "/v1/messages", "/v1/messages/count_tokens"])
def test_new_protocols_reject_invalid_auth(client, path):
    response = client.post(path, headers={"Authorization": "Bearer invalid"}, json={})
    assert response.status_code == 401
    assert client.app.state.sessions.live_count == 0


def test_explicit_response_forks_do_not_share_history(client):
    response = client.post("/v1/responses", json={"model": "auto", "input": "START"}).json()
    def fork(marker):
        result = client.post("/v1/responses", json={"model": "auto", "previous_response_id": response["id"],
                             "input": "INSPECT_CONTEXT:" + marker}).json()
        return json.loads(result["output"][0]["content"][0]["text"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = list(pool.map(fork, ["BRANCH_LEFT", "BRANCH_RIGHT"]))
    assert left["pid"] != right["pid"]
    assert "BRANCH_RIGHT" not in json.dumps(left["history"])
    assert "BRANCH_LEFT" not in json.dumps(right["history"])
