"""Translate the proxy's canonical OpenAI-shaped messages to the 1.4.2
GetChatMessageRequest protobuf JSON and back.

The wire format is documented by the embedded FileDescriptorProto of the
pinned build.  Only fields verified against a real authenticated bridge run
are used here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..contracts import ToolContract, ToolCall
from ..structured_output import ResponseFormat
from .models import EFFORT_LEVELS, model_id


class BridgeProtocolError(ValueError):
    pass


_IMAGE_MARKER = re.compile(r"\[Image attachment image_(\d+); detail=[^\]]+\]")


def _image_media(content: str, images, used: set[int]) -> list[dict]:
    if images is None or not getattr(images, "attachments", None):
        if _IMAGE_MARKER.search(content):
            raise BridgeProtocolError("image attachment is missing from the request")
        return []
    media = []
    for match in _IMAGE_MARKER.finditer(content):
        index = int(match.group(1)) - 1
        if not 0 <= index < len(images.attachments):
            raise BridgeProtocolError("image attachment index is invalid")
        attachment = images.attachments[index]
        media.append({
            "mimeType": attachment.mime,
            "inlineData": base64.b64encode(attachment.data).decode("ascii"),
        })
        used.add(index)
    return media


@dataclass
class BridgeRequest:
    payload: dict
    tools: dict[str, ToolContract] = field(default_factory=dict)
    structured: ResponseFormat | None = None
    synthetic_tool: str | None = None
    wants_tool_calls: bool = False


def _system_prompt(messages: list[dict]) -> tuple[str, list[dict]]:
    parts: list[str] = []
    rest: list[dict] = []
    for message in messages:
        if message.get("role") in ("system", "developer"):
            text = message.get("content")
            if isinstance(text, str) and text:
                parts.append(text)
        else:
            rest.append(message)
    return "\n\n".join(parts), rest


def _tool_choice(choice: Any) -> dict | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        if choice in ("auto", "none", "required"):
            return {"optionName": choice}
        return {"toolName": choice}
    if isinstance(choice, dict):
        function = choice.get("function")
        if choice.get("type") == "function" and isinstance(function, dict) and function.get("name"):
            return {"toolName": function["name"]}
    raise BridgeProtocolError("unsupported tool_choice")


def _tool_definitions(contracts: dict[str, ToolContract]) -> list[dict]:
    result = []
    for name, contract in contracts.items():
        result.append({
            "name": name,
            "description": contract.original_schema.get("description", "")
            if isinstance(contract.original_schema, dict) else "",
            "jsonSchemaString": json.dumps(contract.schema, separators=(",", ":"), sort_keys=True),
        })
    return result


def build_request(
    messages: list[dict],
    *,
    tools: dict[str, ToolContract] | None,
    tool_choice: Any,
    model: str | None,
    effort: str | None,
    conversation_id: str | None,
    response_format: ResponseFormat | None = None,
    max_tokens: int | None = None,
    parallel_tool_calls: bool | None = None,
    signature_lookup=None,
    images=None,
) -> BridgeRequest:
    """Build a minimal protojson request.  `messages` is the normalized
    OpenAI-shaped list produced by ExternalTools._messages."""
    try:
        internal_model = model_id(model)
    except ValueError as exc:
        raise BridgeProtocolError(str(exc)) from None

    system, chat = _system_prompt(messages)
    prompts: list[dict] = []
    used_images: set[int] = set()
    for message in chat:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "user":
            item: dict[str, Any] = {"source": 1, "prompt": content}
            media = _image_media(content, images, used_images)
            if media:
                item["media"] = media
            prompts.append(item)
        elif role == "assistant":
            item: dict[str, Any] = {"source": 2, "prompt": content}
            calls = []
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                calls.append({
                    "id": call.get("id") or "",
                    "name": function.get("name") or "",
                    "argumentsJson": function.get("arguments") or "{}",
                })
            if calls:
                item["toolCalls"] = calls
                if signature_lookup is not None:
                    message_signature = signature_lookup("assistant:" + calls[0]["id"])
                    if message_signature:
                        item["thinkingSignature"] = message_signature
                    for call in calls:
                        signature = signature_lookup(call["id"])
                        if signature:
                            call["thinkingSignature"] = signature
            prompts.append(item)
        elif role == "tool":
            text = content
            is_error = False
            marker = "[Tool execution failed]\n"
            if text.startswith(marker):
                text = text[len(marker):]
                is_error = True
            item = {"source": 4, "prompt": text,
                    "toolCallId": message.get("tool_call_id") or ""}
            if is_error:
                item["toolResultIsError"] = True
            prompts.append(item)
        else:
            raise BridgeProtocolError(f"unsupported message role {role!r}")

    if images is not None and len(used_images) != len(getattr(images, "attachments", ())):
        raise BridgeProtocolError("not every image attachment was referenced by a user prompt")

    contracts = tools or {}
    synthetic = None
    if response_format is not None:
        if contracts:
            raise BridgeProtocolError("response_format cannot be combined with tools")
        synthetic = "structured_output"
        schema = response_format.output_schema
        contracts = {
            synthetic: ToolContract.build(synthetic, schema, original_schema=schema),
        }
        tool_choice = "required"
        tools = contracts

    payload: dict[str, Any] = {
        "metadata": {},
        "internalChatModel": internal_model,
        "requestType": 5,
        "chatMessagePrompts": prompts,
        "configuration": {"numCompletions": "1"},
    }
    if system:
        payload["prompt"] = system
    if conversation_id:
        payload["conversationId"] = conversation_id
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            raise BridgeProtocolError("max_tokens must be a positive integer")
        payload["configuration"]["maxTokens"] = str(max_tokens)
    if effort:
        level = EFFORT_LEVELS.get(effort)
        if level is None:
            # xhigh/max are CLI aliases for the highest advertised level.
            level = 3
        payload["configuration"]["thinkingLevel"] = level
    if parallel_tool_calls is False:
        payload["configuration"]["disableParallelToolCalls"] = True
    definitions = _tool_definitions(contracts)
    if definitions:
        payload["tools"] = definitions
        choice = _tool_choice(tool_choice)
        if choice:
            payload["toolChoice"] = choice
    return BridgeRequest(payload, contracts, response_format, synthetic, bool(definitions))


def conversation_id_for(key: object) -> str:
    text = str(key or "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return str(uuid.UUID(digest[:32]))


@dataclass
class _ToolState:
    id: str
    name: str = ""
    arguments: str = ""
    signature: str | None = None

    def add(self, name: str | None, arguments: str | None, signature: str | None = None) -> None:
        if name:
            self.name = name
        if signature:
            self.signature = signature
        if arguments:
            candidate = self.arguments + arguments
            try:
                json.loads(candidate)
                self.arguments = candidate
            except (ValueError, TypeError):
                if arguments.startswith("{"):
                    self.arguments = arguments
                else:
                    self.arguments = candidate


class ToolAccumulator:
    """Accumulate response Field 6 while keeping the last complete JSON."""

    def __init__(self) -> None:
        self._calls: dict[str, _ToolState] = {}
        self._order: list[str] = []

    def feed(self, calls) -> None:
        if not isinstance(calls, list):
            return
        for index, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            call_id = call.get("id") or f"call_bridge_{len(self._order)}_{index}"
            state = self._calls.get(call_id)
            if state is None:
                state = _ToolState(call_id)
                self._calls[call_id] = state
                self._order.append(call_id)
            state.add(call.get("name"), call.get("argumentsJson"),
                      call.get("thinkingSignature"))

    def result(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for call_id in self._order:
            state = self._calls[call_id]
            if not state.name:
                continue
            try:
                arguments = json.loads(state.arguments or "{}")
            except (ValueError, TypeError):
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(ToolCall(state.name, arguments, id=call_id))
        return calls

    def signatures(self) -> dict[str, str]:
        return {call_id: state.signature for call_id, state in self._calls.items()
                if state.signature}

    def first_id(self) -> str | None:
        return self._order[0] if self._order else None


def usage_from_response(usage: dict | None) -> dict:
    if not isinstance(usage, dict):
        return {}
    def number(name: str) -> int:
        try:
            return int(usage.get(name) or 0)
        except (TypeError, ValueError):
            return 0
    input_tokens = number("inputTokens")
    output_tokens = number("outputTokens")
    cached = number("cacheReadTokens")
    thinking = number("thinkingOutputTokens")
    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cached:
        result["cache_read_tokens"] = cached
    if thinking:
        result["thinking_tokens"] = thinking
    return result


STOP_REASONS = {
    "STOP_REASON_STOP_PATTERN": "stop",
    "STOP_REASON_MAX_TOKENS": "length",
    "STOP_REASON_FUNCTION_CALL": "tool_calls",
    "STOP_REASON_CONTENT_FILTER": "content_filter",
    "STOP_REASON_INCOMPLETE": "length",
    "STOP_REASON_CLIENT_CANCELED": "stop",
    "STOP_REASON_OTHER": "stop",
}


def finish_reason(stop_reason: str | None, has_tools: bool) -> str:
    if has_tools:
        return "tool_calls"
    return STOP_REASONS.get(stop_reason or "", "stop")
