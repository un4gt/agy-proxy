"""Cached CLI input must survive protocol conversion and client subtraction."""
import json
import sys
from contextlib import aclosing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.agy import AgySession
from app.config import Config
from app.token_usage import inclusive_counts


# Real failing Pi session, successful turn 13. CLI total excludes cache reads.
CLI_USAGE = {"input_tokens": 3152, "cache_read_tokens": 14126,
             "output_tokens": 99, "thinking_tokens": 15, "total_tokens": 3251}


@pytest.mark.parametrize("raw, expected", [
    ({}, (0, 0, 0)),
    ({"input_tokens": 100, "output_tokens": 27}, (100, 27, 127)),
    ({"input_tokens": 278, "cache_read_tokens": 30214, "output_tokens": 4}, (30492, 4, 30496)),
    (CLI_USAGE, (17278, 99, 17377)),
])
def test_inclusive_counts(raw, expected):
    assert inclusive_counts(raw) == expected


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages", "gemini"])
@pytest.mark.parametrize("stream", [False, True])
def test_cached_usage_through_http(tmp_path, monkeypatch, protocol, stream):
    original = AgySession.send_turn

    async def send_turn(session, prompt):
        async with aclosing(original(session, prompt)) as events:
            async for event in events:
                if event["event"] == "result":
                    event["result"]["usage"] = dict(CLI_USAGE)
                yield event

    monkeypatch.setattr(AgySession, "send_turn", send_turn)
    fake = Path(__file__).with_name("fake_tools_agy.py").as_posix()
    cfg = Config(agy_bin=f"{sys.executable} {fake}", data_dir=str(tmp_path), api_keys=("test",))
    body = {"model": "auto", "stream": stream}
    if protocol == "responses":
        url = "/v1/responses"
        body["input"] = "hello"
    elif protocol == "gemini":
        url = "/v1beta/models/auto:" + ("streamGenerateContent?alt=sse" if stream else "generateContent")
        body = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
    else:
        url = "/v1/" + ("chat/completions" if protocol == "chat" else "messages")
        body["messages"] = [{"role": "user", "content": "hello"}]
        if protocol == "chat":
            body["stream_options"] = {"include_usage": True}
        else:
            body["max_tokens"] = 1000
    with TestClient(create_app(cfg)) as client:
        response = client.post(url, json=body, headers={"Authorization": "Bearer test"})
        assert response.status_code == 200, response.text
        if stream:
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            if protocol == "responses":
                final = next(e["response"] for e in events if e.get("type") == "response.completed")
            else:
                final = next(e for e in reversed(events) if e.get("usage") or e.get("usageMetadata"))
        else:
            final = response.json()
        usage = final.get("usage", final.get("usageMetadata"))
        if protocol == "chat":
            assert usage == {"prompt_tokens": 17278, "completion_tokens": 99, "total_tokens": 17377,
                             "prompt_tokens_details": {"cached_tokens": 14126},
                             "completion_tokens_details": {"reasoning_tokens": 15}}
            # Pi derives uncached input this way; it must recover the CLI count.
            assert usage["prompt_tokens"] - usage["prompt_tokens_details"]["cached_tokens"] == 3152
        elif protocol == "responses":
            assert usage == {"input_tokens": 17278, "output_tokens": 99, "total_tokens": 17377,
                             "input_tokens_details": {"cached_tokens": 14126},
                             "output_tokens_details": {"reasoning_tokens": 15}}
        elif protocol == "gemini":
            assert usage == {"promptTokenCount": 17278, "candidatesTokenCount": 84,
                             "thoughtsTokenCount": 15, "cachedContentTokenCount": 14126,
                             "totalTokenCount": 17377}
        else:
            assert usage == {"input_tokens": 3152, "output_tokens": 99,
                             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 14126}
