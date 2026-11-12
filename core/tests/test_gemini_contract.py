"""Gemini request/response fidelity beyond the basic bridge wiring."""

from __future__ import annotations

import pytest

from agy_proxy_core.agy import AgyError
from agy_proxy_core.gemini_adapter import result_body, translate
from agy_proxy_core.runner import TurnResult


def _body(**extra):
    body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    body.update(extra)
    return body


def test_max_output_tokens_and_empty_modalities():
    chat = translate(_body(generationConfig={"maxOutputTokens": 64, "responseModalities": []}),
                     model="m")
    assert chat["max_tokens"] == 64


def test_validated_allowed_functions_are_accepted():
    body = _body(
        contents=[{"role": "user", "parts": [{"text": "go"}]}],
        tools=[{"functionDeclarations": [{"name": "write", "parameters": {"type": "object"}}]}],
        toolConfig={"functionCallingConfig": {
            "mode": "VALIDATED", "allowedFunctionNames": ["write"]}},
    )
    chat = translate(body, model="m")
    assert chat["tools"][0]["function"]["name"] == "write"


def test_response_schema_requires_json_mime():
    with pytest.raises(AgyError):
        translate(_body(generationConfig={"responseSchema": {"type": "object"}}), model="m")


def test_multimodal_function_response_is_rejected():
    body = _body(
        contents=[
            {"role": "user", "parts": [{"text": "go"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "write", "args": {}}}]},
            {"role": "user", "parts": [{"functionResponse": {
                "name": "write", "response": {}, "parts": [{"inlineData": {}}]}}]},
        ],
    )
    with pytest.raises(AgyError):
        translate(body, model="m")


def test_length_maps_to_max_tokens():
    body = result_body(TurnResult(text="x", stop_reason="length"), model="m", response_id="id")
    assert body["candidates"][0]["finishReason"] == "MAX_TOKENS"
