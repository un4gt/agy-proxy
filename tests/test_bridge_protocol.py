"""Unit tests for the native bridge request/response translation."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bridge.models import DEFAULT_MODEL_ID, model_id
from app.bridge.protocol import (BridgeProtocolError, ToolAccumulator,
                                 build_request, conversation_id_for,
                                 usage_from_response)
from app.bridge.session import BridgeSessionManager
from app.config import Config
from app.runner import TurnResult
from app.contracts import ToolContract
from app.structured_output import ResponseFormat


def _tool(name="write", schema=None):
    schema = schema or {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"]}
    return ToolContract.build(name, schema, original_schema=schema)


def _messages():
    return [
        {"role": "system", "content": "system one"},
        {"role": "developer", "content": "system two"},
        {"role": "user", "content": "make a file"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {
                "name": "write", "arguments": json.dumps({"path": "a.txt"})}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "wrote a.txt"},
        {"role": "user", "content": "now answer"},
    ]


def test_model_ids_are_pinned_to_the_catalog():
    assert model_id("gemini-3.8-flash-medium") == 1319
    assert model_id("gemini-3.8-flash-high") == 1318
    assert model_id("gemini-3.8-flash-low") == 1320
    assert model_id("gemini-3.8-flash") == 1319
    assert model_id("gemini-4-argon") == 1418
    assert model_id(None) == DEFAULT_MODEL_ID
    with pytest.raises(ValueError):
        model_id("not-a-model")


def test_request_preserves_system_history_tools_and_choice():
    tool = _tool()
    request = build_request(
        _messages(), tools={"write": tool}, tool_choice="required",
        model="gemini-3.8-flash-medium", effort="medium",
        conversation_id="conv-1", max_tokens=256,
        signature_lookup=lambda key: "sig-" + key
        if key in ("call_1", "assistant:call_1") else None,
    )
    payload = request.payload
    assert payload["internalChatModel"] == 1319
    assert payload["prompt"] == "system one\n\nsystem two"
    assert payload["requestType"] == 5
    assert payload["conversationId"] == "conv-1"
    assert payload["configuration"] == {
        "numCompletions": "1", "maxTokens": "256", "thinkingLevel": 2}
    sources = [item["source"] for item in payload["chatMessagePrompts"]]
    assert sources == [1, 2, 4, 1]
    assistant = payload["chatMessagePrompts"][1]
    assert assistant["toolCalls"] == [{
        "id": "call_1", "name": "write",
        "argumentsJson": json.dumps({"path": "a.txt"}),
        "thinkingSignature": "sig-call_1",
    }]
    assert assistant["thinkingSignature"] == "sig-assistant:call_1"
    assert payload["tools"][0]["name"] == "write"
    assert json.loads(payload["tools"][0]["jsonSchemaString"])["required"] == ["path"]
    assert payload["toolChoice"] == {"optionName": "required"}


def test_named_tool_choice_and_tool_error_marker():
    tool = _tool()
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_x", "type": "function", "function": {
                "name": "write", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_x",
         "content": "[Tool execution failed]\npermission denied"},
    ]
    request = build_request(
        messages, tools={"write": tool},
        tool_choice={"type": "function", "function": {"name": "write"}},
        model="gemini-3.8-flash-medium", effort=None,
        conversation_id="c", max_tokens=None)
    assert request.payload["toolChoice"] == {"toolName": "write"}
    result = request.payload["chatMessagePrompts"][2]
    assert result["prompt"] == "permission denied"
    assert result["toolResultIsError"] is True


def test_structured_json_object_uses_the_wrapped_schema():
    fmt = ResponseFormat.from_body({"response_format": {"type": "json_object"}})
    request = build_request(
        [{"role": "user", "content": "give json"}], tools=None,
        tool_choice=None, model="gemini-3.8-flash-medium", effort=None,
        conversation_id=None, response_format=fmt, max_tokens=64)
    assert request.structured is fmt
    assert request.synthetic_tool == "structured_output"
    schema = json.loads(request.payload["tools"][0]["jsonSchemaString"])
    assert schema["properties"]["json"]["type"] == "string"
    assert request.payload["toolChoice"] == {"optionName": "required"}


def test_tool_accumulator_joins_fragments_and_keeps_signature():
    acc = ToolAccumulator()
    acc.feed([{"id": "call_1", "name": "write", "argumentsJson": '{"path":',
               "thinkingSignature": "sig"}])
    acc.feed([{"id": "call_1", "argumentsJson": '"a.txt"}'}])
    calls = acc.result()
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "write"
    assert calls[0].arguments == {"path": "a.txt"}
    assert acc.signatures() == {"call_1": "sig"}
    assert acc.first_id() == "call_1"


def test_turn_result_preserves_length_stop_reason():
    assert TurnResult(text="x", stop_reason="length").finish_reason == "length"
    assert TurnResult(text="x").finish_reason == "stop"


def test_image_marker_becomes_inline_media():
    images = SimpleNamespace(attachments=[
        SimpleNamespace(mime="image/png", data=b"png-bytes")])
    request = build_request(
        [{"role": "user", "content":
          "what is this?\n[Image attachment image_1; detail=auto]\n"}],
        tools=None, tool_choice=None, model="gemini-3.8-flash-medium",
        effort=None, conversation_id=None, images=images)
    media = request.payload["chatMessagePrompts"][0]["media"]
    assert media == [{"mimeType": "image/png", "inlineData": "cG5nLWJ5dGVz"}]


def test_image_marker_without_attachment_is_rejected():
    with pytest.raises(BridgeProtocolError):
        build_request(
            [{"role": "user", "content":
              "x\n[Image attachment image_1; detail=auto]\n"}],
            tools=None, tool_choice=None, model="gemini-3.8-flash-medium",
            effort=None, conversation_id=None, images=SimpleNamespace(attachments=[]))


def test_usage_mapping_keeps_thinking_and_cache():
    usage = usage_from_response({
        "inputTokens": "10", "outputTokens": "20",
        "thinkingOutputTokens": "5", "cacheReadTokens": "3"})
    assert usage == {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
                     "cache_read_tokens": 3, "thinking_tokens": 5}


def test_conversation_id_is_stable_and_uuid_shaped():
    one = conversation_id_for("same-session")
    two = conversation_id_for("same-session")
    assert one == two
    assert len(one) == 36
    assert conversation_id_for("other") != one


def test_response_format_with_tools_is_rejected():
    fmt = ResponseFormat.from_body({"response_format": {"type": "json_object"}})
    with pytest.raises(BridgeProtocolError):
        build_request([{"role": "user", "content": "x"}],
                      tools={"write": _tool()}, tool_choice="auto",
                      model="gemini-3.8-flash-medium", effort=None,
                      conversation_id=None, response_format=fmt)


def test_signature_cache_is_bounded(tmp_path):
    manager = BridgeSessionManager(Config(data_dir=str(tmp_path), bridge=True))
    for i in range(4100):
        manager.remember_signature(f"call_{i}", f"sig_{i}")
    assert manager.signature("call_0") is None
    assert manager.signature("call_4099") == "sig_4099"
    assert len(manager._signatures) == 4096


def test_signature_cache_survives_manager_restart(tmp_path):
    cfg = Config(data_dir=str(tmp_path), bridge=True)
    first = BridgeSessionManager(cfg)
    first.remember_signature("call_1", "sig-call-1")
    first.remember_signature("assistant:call_1", "sig-assistant")
    first.flush_signatures()

    second = BridgeSessionManager(cfg)
    assert second.signature("call_1") == "sig-call-1"
    assert second.signature("assistant:call_1") == "sig-assistant"
    assert (tmp_path / "bridge-signatures.json").stat().st_mode & 0o777 == 0o600


def test_production_patcher_reproduces_the_pinned_sha():
    source = Path(__file__).resolve().parents[1] / ".devdata/agy-1.4.2-image"
    if not source.is_file():
        pytest.skip("pinned 1.4.2 image is not present")
    from app.bridge.patcher import apply
    data, plan = apply(source.read_bytes())
    assert plan["sha256"] == "636570d016f462b68382781917146ec8756dce416895bdfecfdd151f0a060649"
    assert len(data) > 0
