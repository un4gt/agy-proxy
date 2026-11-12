"""Bridge turn translation without a patched CLI host."""

from __future__ import annotations

import asyncio
import json

from agy_proxy_core.bridge.turn import BridgeTurn
from agy_proxy_core.config import Config
from agy_proxy_core.external_tools import ExternalTools
from agy_proxy_core.runner import TurnProfile


class FakeSession:
    account_id = "acct-1"
    account_name = "Test Account"
    turns = 0

    def __init__(self, events):
        self.events = events
        self.payload = None
        self.closed = False

    async def stream(self, payload):
        self.payload = payload
        for event in self.events:
            yield event


class FakeManager:
    def __init__(self, session):
        self.session = session
        self.acquired = None
        self.released = False
        self.signatures = {}
        self.flushed = False

    async def acquire(self, **kwargs):
        self.acquired = kwargs
        return self.session

    async def release(self, session):
        self.released = True

    def signature(self, key):
        return self.signatures.get(key)

    def remember_signature(self, key, value):
        if value:
            self.signatures[key] = value

    def flush_signatures(self):
        self.flushed = True


def _external(messages, tools=None):
    body = {"messages": messages}
    if tools is not None:
        body["tools"] = tools
    else:
        body["tools"] = []
    return ExternalTools.from_body(body)


def _collect(turn):
    async def run():
        return [event async for event in turn]
    return asyncio.run(run())


def test_text_deltas_usage_and_stop_reason_are_forwarded():
    session = FakeSession([
        {"deltaText": "hello "},
        {"deltaText": "world"},
        {"usage": {"inputTokens": "9", "outputTokens": "3"}},
        {"stopReason": "STOP", "messageId": "msg-1"},
    ])
    manager = FakeManager(session)
    turn = BridgeTurn(
        Config(turn_timeout=5), manager,
        external=_external([{"role": "user", "content": "hi"}]),
        response_format=None, model="gemini-3.8-flash-medium", effort="medium",
        profile=TurnProfile(), session_key="owner-1",
    )
    events = _collect(turn)
    assert events[0] == ("ready", None)
    assert [value for kind, value in events[1:3]] == ["hello ", "world"]
    result = events[3][1]
    assert result.text == "hello world"
    assert result.usage == {"input_tokens": 9, "output_tokens": 3, "total_tokens": 12}
    assert result.stop_reason == "stop"
    assert manager.released is True
    assert session.payload["chatMessagePrompts"][0]["prompt"] == "hi"


def test_tool_calls_are_reassembled_and_signatures_persist():
    args = json.dumps({"path": "a.txt"})
    session = FakeSession([
        {"deltaToolCalls": [{"id": "call_1", "name": "write",
                             "argumentsJson": args[:8],
                             "thinkingSignature": "sig-call"}]},
        {"deltaToolCalls": [{"id": "call_1", "argumentsJson": args[8:]}]},
        {"deltaSignature": "sig-message"},
        {"usage": {"inputTokens": "10", "outputTokens": "4"}},
        {"stopReason": "STOP"},
    ])
    manager = FakeManager(session)
    tools = [{"type": "function", "function": {
        "name": "write", "description": "write a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}}]
    turn = BridgeTurn(
        Config(turn_timeout=5), manager,
        external=_external([{"role": "user", "content": "write it"}], tools),
        response_format=None, model="gemini-3.8-flash-medium", effort=None,
        profile=TurnProfile(), session_key="owner-1",
    )
    events = _collect(turn)
    result = events[-1][1]
    assert len(result.calls) == 1
    assert result.calls[0].name == "write"
    assert result.calls[0].arguments == {"path": "a.txt"}
    assert manager.signatures == {
        "call_1": "sig-call",
        "assistant:call_1": "sig-message",
    }
    assert manager.flushed is True
    assert session.payload["tools"][0]["name"] == "write"


def test_structured_output_is_validated_and_unwrapped():
    session = FakeSession([
        {"deltaToolCalls": [{"id": "call_s", "name": "structured_output",
                             "argumentsJson": json.dumps({"name": "Alice", "age": 30})}]},
        {"usage": {"inputTokens": "5", "outputTokens": "6"}},
        {"stopReason": "STOP"},
    ])
    manager = FakeManager(session)
    turn = BridgeTurn(
        Config(turn_timeout=5), manager,
        external=None, response_format=_format(), model="gemini-3.8-flash-medium",
        effort=None, profile=TurnProfile(), session_key="owner-1",
    )
    result = _collect(turn)[-1][1]
    assert json.loads(result.text) == {"name": "Alice", "age": 30}
    assert result.calls == []


def _format():
    from agy_proxy_core.structured_output import ResponseFormat
    return ResponseFormat.from_body({"response_format": {"type": "json_schema", "json_schema": {
        "name": "person", "schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name", "age"], "additionalProperties": False}}}})
