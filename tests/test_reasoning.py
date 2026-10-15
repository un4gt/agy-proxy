"""Reasoning selection verified at the HTTP boundary and real child argv."""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import agy, create_app, protocol_common
from app.agy import AgyError
from app.config import Config


@pytest.fixture
def client(tmp_path, monkeypatch):
    trace = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_AGY_ARGV_FILE", str(trace))
    fake = (Path(__file__).parent / "fake_tools_agy.py").as_posix()
    cfg = Config(agy_bin=f"{sys.executable} {fake}", data_dir=str(tmp_path / "data"),
                 model_map={"custom": "private-model", "fast": "gemini-3-flash-medium"},
                 default_model="gemini-3-pro-high")
    agy._model_caches.clear()
    with TestClient(create_app(cfg)) as client:
        client.trace = trace
        yield client


def argv(client):
    return json.loads(client.trace.read_text().splitlines()[-1])["argv"]


def request(client, **kwargs):
    return client.post("/v1/chat/completions", json={
        "model": "gemini-3-pro", "messages": [{"role": "user", "content": "PING"}], **kwargs,
    })


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["text", "tools", "json"])
def test_base_model_and_effort_reach_real_child_in_all_modes(client, stream, mode):
    options = {"reasoning_effort": "high", "stream": stream}
    if mode == "tools":
        options["tools"] = [{"type": "function", "function": {
            "name": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        }}]
    if mode == "json":
        options["response_format"] = {"type": "json_object"}
    response = request(client, **options)
    assert response.status_code == 200, response.text
    args = argv(client)
    assert args[args.index("--model") + 1] == "gemini-3-pro-high"
    assert args[args.index("--effort") + 1] == "high"
    if stream:
        chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        assert all(c["model"] == "gemini-3-pro" for c in chunks)
        assert response.text.count("data: [DONE]") == 1
    else:
        assert response.json()["model"] == "gemini-3-pro"
        if mode == "tools":
            assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
        elif mode == "json":
            assert json.loads(response.json()["choices"][0]["message"]["content"]) == {"name": "Budi", "age": 20}
    assert client.app.state.sessions.live_count == 1
    assert client.app.state.sessions.busy_count() == 0


@pytest.mark.parametrize("model,effort,expected", [
    ("gemini-3-pro-low", "high", "gemini-3-pro-high"),
    ("gemini-3-pro-high", "low", "gemini-3-pro-low"),
    ("gemini-3-pro", "minimal", "gemini-3-pro-low"),
    ("gemini-3-flash", "medium", "gemini-3-flash-medium"),
    ("fast", "low", "gemini-3-flash-low"),
    ("default", "low", "gemini-3-pro-low"),
    ("auto", "max", "auto"),
    ("custom", "xhigh", "private-model"),
    ("gemini-3-pro", " HIGH ", "gemini-3-pro-high"),
])
def test_explicit_effort_wins_and_aliases_work(client, model, effort, expected):
    response = request(client, model=model, reasoning_effort=effort)
    assert response.status_code == 200, response.text
    args = argv(client)
    assert args[args.index("--model") + 1] == expected
    assert args[args.index("--effort") + 1] == ("low" if effort == "minimal" else effort.strip().lower())


@pytest.mark.parametrize("effort", [None, "low", "high"])
def test_server_default_and_request_override(client, effort):
    client.app.state.cfg.effort = "high"
    response = request(client, model="gemini-3-pro-low", reasoning_effort=effort)
    assert response.status_code == 200, response.text
    expected = effort or "high"
    args = argv(client)
    assert args[args.index("--model") + 1] == "gemini-3-pro-" + expected
    assert args[args.index("--effort") + 1] == expected


def test_omitting_effort_preserves_concrete_model(client):
    assert request(client, model="gemini-3-pro-high").status_code == 200
    args = argv(client)
    assert args[args.index("--model") + 1] == "gemini-3-pro-high"
    assert "--effort" not in args


@pytest.mark.parametrize("value", ["", "ultra", "none", "medium", "xhigh", "max", 2, True, {}, []])
@pytest.mark.parametrize("stream", [False, True])
def test_invalid_or_unsupported_effort_is_400_before_any_turn(client, value, stream):
    response = request(client, reasoning_effort=value, stream=stream)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["param"] == "reasoning_effort"
    assert "text/event-stream" not in response.headers["content-type"]
    assert not client.trace.exists()
    if value == "medium":
        assert "low, high" in error["message"]


def test_future_advertised_effort_is_selected(client, monkeypatch):
    async def models(_cfg, **_kwargs):
        return ["future-model-low", "future-model-xhigh", "future-model-max"]
    monkeypatch.setattr(protocol_common, "list_models", models)
    for effort in ("xhigh", "max"):
        response = request(client, model="future-model", reasoning_effort=effort)
        assert response.status_code == 200, response.text
        args = argv(client)
        assert args[args.index("--model") + 1] == "future-model-" + effort


def test_catalog_failure_does_not_guess_effort(client, monkeypatch):
    async def unavailable(_cfg, **_kwargs):
        raise AgyError("model discovery unavailable", status_code=503)
    monkeypatch.setattr(protocol_common, "list_models", unavailable)
    response = request(client, reasoning_effort="high")
    assert response.status_code == 503
    assert not client.trace.exists()


def test_unknown_model_stays_404(client):
    response = request(client, model="not-a-model", reasoning_effort="high")
    assert response.status_code == 404
    assert response.json()["error"]["param"] == "model"
    assert not client.trace.exists()


def test_invalid_server_default_is_config_error(client):
    client.app.state.cfg.effort = "invalid"
    response = request(client)
    assert response.status_code == 500
    assert response.json()["error"]["type"] == "server_error"
    assert not client.trace.exists()


def test_model_discovery_includes_base_names_and_original_slugs(client):
    models = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert "gemini-3-pro" in models and "gemini-3-pro-high" in models
    assert len(models) == len(set(models))
    assert client.get("/v1/models/gemini-3-pro").status_code == 200
