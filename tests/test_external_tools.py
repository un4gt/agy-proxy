from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.config import Config


READ = {"type": "function", "function": {"name": "read", "description": "Read a client file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"], "additionalProperties": False}}}


@pytest.fixture
def client(tmp_path):
    fake = (Path(__file__).parent / "fake_tools_agy.py").as_posix()
    # External mode must override these unsafe/conflicting server defaults.
    cfg = Config(agy_bin=f"{sys.executable} {fake}", data_dir=str(tmp_path / "data"),
                 skip_permissions=True, agent="some-other-agent", agy_cwd=str(tmp_path))
    with TestClient(create_app(cfg)) as c:
        yield c


def request(text="READ", **kwargs):
    return {"model": "auto", "tools": [deepcopy(READ)],
            "messages": [{"role": "user", "content": text}], **kwargs}


def send(client, body):
    return client.post("/v1/chat/completions", json=body)


def test_tool_roundtrip_uses_fresh_session_and_preserves_results(client):
    body = request()
    first = send(client, body)
    assert first.status_code == 200, first.text
    choice = first.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    message = choice["message"]
    call = message["tool_calls"][0]
    assert call["type"] == "function" and call["id"].startswith("call_")
    assert json.loads(call["function"]["arguments"]) == {"path": "client.txt"}
    # Pi can reserialize arguments with different whitespace.
    call["function"]["arguments"] = '{ "path" : "client.txt" }'
    body["messages"] += [message, {"role": "tool", "tool_call_id": call["id"], "content": "PI_RESULT_123"}]
    second = send(client, body)
    assert second.status_code == 200, second.text
    result = second.json()["choices"][0]
    assert result["finish_reason"] == "stop"
    assert result["message"]["content"] == "turn=1; READ; ; PI_RESULT_123"
    assert client.app.state.sessions.live_count == 0


def test_multiple_results_and_cold_history(client):
    body = request("MULTI")
    first = send(client, body).json()["choices"][0]["message"]
    calls = first["tool_calls"]
    assert len(calls) == 2 and calls[0]["id"] != calls[1]["id"]
    body["messages"] += [first] + [{"role": "tool", "tool_call_id": c["id"], "content": f"result-{i}"}
                                    for i, c in enumerate(calls)]
    second = send(client, body).json()["choices"][0]["message"]["content"]
    assert second == "turn=1; MULTI; ; result-0; result-1"
    # Changed tool definitions must still receive the complete results.
    body["tools"][0]["function"]["description"] = "Changed description"
    third = send(client, body).json()["choices"][0]["message"]["content"]
    assert "turn=1" in third and "result-0" in third and "result-1" in third


@pytest.mark.parametrize("stream", [False, True])
def test_parallel_cloned_tool_histories_have_private_processes_and_workspaces(client, stream):
    from concurrent.futures import ThreadPoolExecutor

    client.app.state.cfg.api_keys = ("shared-isolation-test-key",)
    client.headers["Authorization"] = "Bearer shared-isolation-test-key"
    base = request()
    assistant = send(client, base).json()["choices"][0]["message"]
    call_id = assistant["tool_calls"][0]["id"]
    # Same credential, tools, assistant prefix and tool-call ID. Only the real
    # client tool result differs between these independently forked sessions.
    def branch(marker):
        body = deepcopy(base)
        body["stream"] = stream
        body["messages"] += [deepcopy(assistant), {"role": "tool", "tool_call_id": call_id,
                                                   "content": "INSPECT_CONTEXT:" + marker}]
        response = send(client, body)
        assert response.status_code == 200, response.text
        if stream:
            chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            content = "".join(c["choices"][0]["delta"].get("content", "")
                              for c in chunks if c.get("choices"))
        else:
            content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    with ThreadPoolExecutor(max_workers=2) as workers:
        left, right = list(workers.map(branch, ["ONLY_LEFT", "ONLY_RIGHT"]))
    assert left["pid"] != right["pid"] and left["cwd"] != right["cwd"]
    for result, own, other in [(left, "ONLY_LEFT", "ONLY_RIGHT"), (right, "ONLY_RIGHT", "ONLY_LEFT")]:
        assert result["turn"] == 1
        assert result["history"][-1]["tool_call_id"] == call_id
        transcript = json.dumps(result["history"])
        assert own in transcript and other not in transcript
        assert not Path(result["cwd"]).exists()  # workspace removed after request
    assert client.app.state.sessions.live_count == 0


@pytest.mark.parametrize("stream", [False, True])
def test_capacity_rejection_is_http_429_and_does_not_kill_active_request(client, stream):
    from functools import partial

    mgr = client.app.state.sessions
    client.app.state.cfg.max_sessions = 1
    reserved = client.portal.call(partial(mgr.acquire, model='auto', effort=None))
    try:
        response = send(client, request(stream=stream))
        assert response.status_code == 429, response.text
        assert response.json()['error']['type'] == 'rate_limit_error'
        assert reserved.alive and mgr.busy_count() == 1
    finally:
        client.portal.call(mgr.drop, reserved)
    assert send(client, request(stream=stream)).status_code == 200


def test_streamed_tool_calls_have_indexes_and_no_internal_json(client):
    response = send(client, request("MULTI", stream=True, stream_options={"include_usage": True}))
    assert response.status_code == 200
    chunks = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    choices = [c["choices"][0] for c in chunks if c.get("choices")]
    assert "".join(c["delta"].get("content", "") for c in choices) == ""
    calls = [call for c in choices for call in c["delta"].get("tool_calls", [])]
    assert [c["index"] for c in calls] == [0, 1]
    assert choices[-1]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"]["total_tokens"] > 0
    assert response.text.count("data: [DONE]") == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("arguments", [
    {"path": "D:\\workspace\\NirsWisdom-Next\\index.html"},
    {"path": "teaching.js", "offset": 1, "limit": 100},
])
def test_first_read_accepts_object_arguments(client, stream, arguments):
    read = deepcopy(READ)
    read["function"]["parameters"]["properties"].update({
        "offset": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1},
    })
    body = request("ECHO_ARGUMENTS\n" + json.dumps(arguments), tools=[read], stream=stream)
    response = send(client, body)
    assert response.status_code == 200, response.text
    if stream:
        chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        calls = [call for chunk in chunks for choice in chunk.get("choices", [])
                 for call in choice["delta"].get("tool_calls", [])]
    else:
        calls = response.json()["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read"
    assert json.loads(calls[0]["function"]["arguments"]) == arguments


@pytest.mark.parametrize("stream", [False, True])
def test_object_arguments_preserve_file_content_and_roundtrip(client, stream):
    content = (
        '<!doctype html>\r\n<html lang="zh-CN">\n'
        '<script>const config = {"path": "C:\\\\project\\\\index.html"};\n'
        'const template = `first line\n${config.path}\nsecond line`;\n'
        'const pattern = /\\s+\\n/g;</script>\n'
        '<p title="他说：\'你好\'">测试页面 🧪\t1920 × 1080</p>\n</html>'
    ) * 60
    arguments = {"path": "C:\\project\\index-v2.html", "content": content}
    write = {"type": "function", "function": {"name": "write", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"], "additionalProperties": False,
    }}}
    body = request("ECHO_ARGUMENTS\n" + json.dumps(arguments), tools=[write], stream=stream)
    response = send(client, body)
    assert response.status_code == 200, response.text
    if stream:
        chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        choices = [chunk["choices"][0] for chunk in chunks if chunk.get("choices")]
        calls = [call for choice in choices for call in choice["delta"].get("tool_calls", [])]
        assert choices[-1]["finish_reason"] == "tool_calls"
        assert "".join(choice["delta"].get("content", "") for choice in choices) == ""
        message = {"role": "assistant", "content": None, "tool_calls": calls}
    else:
        choice = response.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        message = choice["message"]
        calls = message["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "write"
    assert isinstance(calls[0]["function"]["arguments"], str)
    assert json.loads(calls[0]["function"]["arguments"]) == arguments

    body["stream"] = False
    body["messages"] += [message, {"role": "tool", "tool_call_id": calls[0]["id"], "content": "FILE_WRITTEN"}]
    result = send(client, body)
    assert result.status_code == 200, result.text
    assert result.json()["choices"][0]["message"]["content"] == (
        "turn=1; ECHO_ARGUMENTS\n" + json.dumps(arguments) + "; ; FILE_WRITTEN"
    )


@pytest.mark.parametrize("choice", ["auto", "required", {"type": "function", "function": {"name": "read"}}])
def test_tool_choices(client, choice):
    response = send(client, request(tool_choice=choice))
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.parametrize("kwargs", [{"tool_choice": "none"}, {"tools": []}, {"tools": [], "tool_choice": "auto"}])
def test_no_tools_still_uses_isolated_agent(client, kwargs):
    response = send(client, request(**kwargs))
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in response.json()["choices"][0]["message"]


@pytest.mark.parametrize("text,extra", [
    ("BAD_ARGS", {}), ("BAD_JSON", {}), ("UNKNOWN_TOOL", {}), ("WRONG_KIND", {}),
    ("MALFORMED", {}), ("NATIVE_TOOL", {}), ("NO_CALL", {"tool_choice": "required"}),
    ("MULTI", {"parallel_tool_calls": False}),
    ("JSON_STRING_ARGS", {}), ("ARRAY_ARGS", {}), ("NULL_ARGS", {}),
])
def test_bad_model_output_fails_and_drops_session(client, text, extra):
    response = send(client, request(text, **extra))
    assert response.status_code == 502, response.text
    assert client.app.state.sessions.live_count == 0


@pytest.mark.parametrize("extra", [
    {"tools": "invalid"}, {"tools": [{"type": "custom"}]},
    {"tool_choice": "invalid"}, {"tools": [], "tool_choice": "required"},
    {"tool_choice": {"type": "function", "function": {"name": "missing"}}},
    {"parallel_tool_calls": "false"}, {"n": 2}, {"response_format": {"type": "json_object"}},
    {"messages": [{"role": "tool", "tool_call_id": "orphan", "content": "x"}]},
])
def test_invalid_requests_rejected(client, extra):
    response = send(client, request(**extra))
    assert response.status_code == 400, response.text


def test_unresolved_and_duplicate_tool_results_rejected(client):
    body = request()
    message = send(client, body).json()["choices"][0]["message"]
    body["messages"].append(message)
    assert send(client, body).status_code == 400
    result = {"role": "tool", "tool_call_id": message["tool_calls"][0]["id"], "content": "data"}
    body["messages"] += [result, result]
    assert send(client, body).status_code == 400


@pytest.mark.parametrize("arguments", [{"path": "client.txt"}, "[]", "null", '"client.txt"', "{oops"])
def test_client_history_still_requires_json_encoded_object(client, arguments):
    body = request()
    body["messages"] += [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_invalid_history", "type": "function",
            "function": {"name": "read", "arguments": arguments},
        }]},
        {"role": "tool", "tool_call_id": "call_invalid_history", "content": "data"},
    ]
    response = send(client, body)
    assert response.status_code == 400, response.text
    assert client.app.state.sessions.live_count == 0


def test_parameter_schema_validation_and_local_references(client):
    body = request()
    schema = {"type": "object", "properties": {"path": {"$ref": "#/$defs/path"}},
              "$defs": {"path": {"type": "string"}}, "required": ["path"]}
    body["tools"][0]["function"]["parameters"] = schema
    assert send(client, body).status_code == 200
    schema["properties"]["path"]["$ref"] = "https://example.com/schema.json"
    assert send(client, body).status_code == 400
    body["tools"][0]["function"]["parameters"] = {"type": "not-a-type"}
    assert send(client, body).status_code == 400


@pytest.mark.parametrize("name,decision", [("view_file", "deny"), ("run_command", "deny"),
                                           ("invoke_subagent", "deny"), ("finish", "allow")])
def test_pretool_hook_denies_native_execution(name, decision):
    guard = Path(__file__).parents[1] / "app" / "tool_guard.py"
    response = subprocess.run([sys.executable, str(guard)], input=json.dumps({"toolCall": {"name": name}}),
                              text=True, capture_output=True, check=True)
    assert json.loads(response.stdout)["decision"] == decision
