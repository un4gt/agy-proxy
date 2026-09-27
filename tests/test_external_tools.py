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


def test_tool_roundtrip_reuses_session_and_preserves_results(client):
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
    assert result["message"]["content"] == "turn=2; PI_RESULT_123"


def test_multiple_results_and_cold_history(client):
    body = request("MULTI")
    first = send(client, body).json()["choices"][0]["message"]
    calls = first["tool_calls"]
    assert len(calls) == 2 and calls[0]["id"] != calls[1]["id"]
    body["messages"] += [first] + [{"role": "tool", "tool_call_id": c["id"], "content": f"result-{i}"}
                                    for i, c in enumerate(calls)]
    second = send(client, body).json()["choices"][0]["message"]["content"]
    assert second == "turn=2; result-0; result-1"
    # Changing tools starts a fresh session which must still receive results.
    body["tools"][0]["function"]["description"] = "Changed description"
    third = send(client, body).json()["choices"][0]["message"]["content"]
    assert "turn=1" in third and "result-0" in third and "result-1" in third


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
