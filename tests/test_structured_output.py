from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.config import Config


FORMAT = {"type": "json_schema", "json_schema": {"name": "person", "strict": True, "schema": {
    "type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name", "age"], "additionalProperties": False,
}}}


@pytest.fixture
def client(tmp_path):
    fake = (Path(__file__).parent / "fake_tools_agy.py").as_posix()
    cfg = Config(agy_bin=f"{sys.executable} {fake}", data_dir=str(tmp_path / "data"),
                 default_model="gemini-3-pro-high", model_map={"custom": "private-model"})
    with TestClient(create_app(cfg)) as client:
        yield client


def body(text="Extract Budi, aged 20", **kw):
    return {"model": "gemini-3-pro-high", "messages": [{"role": "user", "content": text}],
            "response_format": deepcopy(FORMAT), **kw}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("fmt", [FORMAT, {"type": "json_object"}])
def test_validated_json_and_stream(client, stream, fmt):
    r = client.post("/v1/chat/completions", json=body(
        stream=stream, stream_options={"include_usage": True}, response_format=fmt))
    assert r.status_code == 200, r.text
    if stream:
        chunks = [json.loads(line[6:]) for line in r.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        text = "".join(c["delta"].get("content", "") for chunk in chunks for c in chunk["choices"])
        assert r.text.count("data: [DONE]") == 1
        assert chunks[-1]["usage"]["total_tokens"] > 0
        assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    else:
        text = r.json()["choices"][0]["message"]["content"]
    assert json.loads(text) == {"name": "Budi", "age": 20}
    assert client.app.state.sessions.live_count == 0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("text", ["BAD_TYPE", "EXTRA_FIELD", "MALFORMED", "NATIVE_TOOL"])
def test_invalid_upstream_output_never_escapes(client, text, stream):
    r = client.post("/v1/chat/completions", json=body(text, stream=stream))
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"
    assert "data: " not in r.text
    assert client.app.state.sessions.live_count == 0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("text", ["JSON_ARRAY", "JSON_INVALID", "MALFORMED"])
def test_json_mode_validates_unwrapped_object(client, text, stream):
    r = client.post("/v1/chat/completions", json=body(
        text, stream=stream, response_format={"type": "json_object"}))
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"
    assert "data: " not in r.text


def test_schema_and_full_conversation_reach_cli(client):
    fmt = {"type": "json_schema", "json_schema": {"name": "echo", "schema": {
        "type": "object", "properties": {"schema": {"type": "object"}, "prompt": {"type": "string"}},
        "required": ["schema", "prompt"], "additionalProperties": False,
    }}}
    r = client.post("/v1/chat/completions", json=body(response_format=fmt, messages=[
        {"role": "system", "content": "Keep names"},
        {"role": "user", "content": "Earlier input"},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": "SCHEMA_ECHO"},
    ]))
    assert r.status_code == 200, r.text
    output = json.loads(r.json()["choices"][0]["message"]["content"])
    assert output["schema"] == fmt["json_schema"]["schema"]
    assert all(s in output["prompt"] for s in ["Keep names", "Earlier input", "Earlier answer", "SCHEMA_ECHO"])


@pytest.mark.parametrize("fmt", [[], {}, {"type": "bogus"}, {"type": "json_schema"},
    {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object", "properties": []}}},
    {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object", "$ref": "https://example.com/schema"}}},
    {"type": "json_schema", "json_schema": {"name": "x", "strict": "true", "schema": {"type": "object"}}},
])
def test_invalid_format_is_client_error(client, fmt):
    r = client.post("/v1/chat/completions", json=body(response_format=fmt))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize("stream", [False, True])
def test_unknown_model_rejected_even_with_default(client, stream):
    r = client.post("/v1/chat/completions", json=body(model="does-not-exist", stream=stream))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
    assert r.json()["error"]["param"] == "model"
    assert client.get("/v1/models/does-not-exist").status_code == 404


@pytest.mark.parametrize("model", ["default", "auto", "custom", "gemini-3-pro"])
def test_explicit_aliases_remain_supported(client, model):
    r = client.post("/v1/chat/completions", json=body(model=model))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("kw", [{"model": []}, {"messages": {}}, {"messages": [1]},
                                {"stream": "false"}, {"stream_options": []},
                                {"stream_options": {"include_usage": "true"}}])
def test_invalid_request_shapes_are_400(client, kw):
    assert client.post("/v1/chat/completions", json=body(**kw)).status_code == 400
