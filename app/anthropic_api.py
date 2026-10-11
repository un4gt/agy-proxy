"""Anthropic Messages wire adapter for text and client-executed tools."""
from __future__ import annotations

from . import json_codec as json
import math
import uuid
from contextlib import aclosing

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from .json_codec import JSONResponse

from .agy import AgyError
from .images import ImageInput, normalize_content
from .auth import is_authorized
from .openai_api import _read_json
from .protocol_common import array, boolean, encode, invalid, obj, optional_obj, prepare, string, text_content

router = APIRouter()


def error_response(status, message, request_id):
    types = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
             404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error",
             503: "overloaded_error", 529: "overloaded_error"}
    return JSONResponse({"type": "error", "error": {"type": types.get(status, "api_error"),
                         "message": message}, "request_id": request_id}, status_code=status,
                        headers={"request-id": request_id})


def translate(body, *, count_only=False):
    messages = []
    system_messages = []
    if body.get("system") is not None:
        system_messages.append({"role": "system", "content": text_content(body["system"], types=("text",))})
    for message in array(body.get("messages"), "messages"):
        message = obj(message, "message")
        role = message.get("role")
        # Claude Code 2.1.295 adds an environment system message after the
        # user turn, alongside the standard top-level system blocks.
        if role == "system":
            system_messages.append({"role": "system", "content": text_content(message.get("content"), types=("text",))})
            continue
        if role not in ("user", "assistant"):
            raise invalid("Messages roles must be user or assistant; use the system field for instructions")
        content = message.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        text, calls, results = [], [], []
        for block in array(content, "message.content"):
            block = obj(block, "content block")
            kind = block.get("type")
            if kind == "text":
                text.append({"type": "text", "text": string(block.get("text"), "text")})
            elif kind == "image" and role == "user":
                text.extend(normalize_content([block], protocol="messages", role=role))
            elif kind == "tool_use" and role == "assistant":
                calls.append({"id": string(block.get("id"), "tool_use.id", nonempty=True), "type": "function",
                              "function": {"name": string(block.get("name"), "tool_use.name"),
                                           "arguments": json.dumps_text(obj(block.get("input"), "tool_use.input"))}})
            elif kind == "tool_result" and role == "user":
                if text:
                    raise invalid("tool_result blocks must precede user text")
                result = text_content(block.get("content", ""), types=("text",))
                if boolean(block, "is_error"):
                    result = "[Tool execution failed]\n" + result
                results.append({"role": "tool", "tool_call_id": string(block.get("tool_use_id"), "tool_use_id"),
                                "content": result})
            elif kind in ("thinking", "redacted_thinking") and role == "assistant":
                # Provider-specific private reasoning/signatures cannot be reused
                # by agy. Preserve visible assistant/tool history, never forge one.
                continue
            else:
                raise invalid(f"Unsupported Messages content block: {kind}")
        messages.extend(results)
        if text or calls or not results:
            item = {"role": role, "content": text}
            if calls:
                item["tool_calls"] = calls
            messages.append(item)
    tools = []
    for tool in array(body.get("tools", []), "tools"):
        tool = obj(tool, "tool")
        if tool.get("type", "custom") not in ("custom", None):
            raise invalid("Only client-defined tools are supported; server tools are unavailable")
        tools.append({"type": "function", "function": {"name": tool.get("name"),
                      "description": tool.get("description", ""), "parameters": tool.get("input_schema")}})
    choice = obj(body.get("tool_choice", {"type": "auto" if tools else "none"}), "tool_choice")
    kind = choice.get("type")
    if kind == "tool":
        translated_choice = {"type": "function", "function": {"name": choice.get("name")}}
    elif kind in ("auto", "any", "none"):
        translated_choice = "required" if kind == "any" else kind
    else:
        raise invalid("Unsupported tool_choice.type")
    output = optional_obj(body, "output_config")
    thinking = optional_obj(body, "thinking")
    if thinking and thinking.get("type") not in ("enabled", "disabled", "adaptive"):
        raise invalid("Unsupported thinking.type")
    effort = output.get("effort")
    if effort is None and thinking.get("type") == "enabled":
        budget = thinking.get("budget_tokens")
        if type(budget) is not int or budget < 1024:
            raise invalid("thinking.budget_tokens must be an integer of at least 1024")
        effort = "high"
    if not count_only:
        maximum = body.get("max_tokens")
        if type(maximum) is not int or maximum <= 0:
            raise invalid("max_tokens must be a positive integer")
    for stop in array(body.get("stop_sequences", []), "stop_sequences"):
        string(stop, "stop_sequence", nonempty=True)
    chat = {"model": body.get("model"), "messages": system_messages + messages, "tools": tools,
            "tool_choice": translated_choice, "parallel_tool_calls": not boolean(choice, "disable_parallel_tool_use"),
            "stream": boolean(body, "stream"), "reasoning_effort": effort}
    if output.get("format") is not None:
        fmt = obj(output["format"], "output_config.format")
        if fmt.get("type") != "json_schema":
            raise invalid("Unsupported output_config.format")
        chat["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "message_output", "schema": fmt.get("schema"), "strict": True}}
    return chat


def message_result(result, model, message_id, stops):
    content = []
    text, stop_sequence = result.text, None
    matches = [(text.index(s), s) for s in stops if s in text]
    if matches and not result.tool_calls:
        index, stop_sequence = min(matches)
        text = text[:index]
    if text:
        content.append({"type": "text", "text": text})
    for call in result.tool_calls:
        content.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                        "input": json.loads(call["function"]["arguments"])})
    usage = result.usage
    return {"id": message_id, "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": "tool_use" if result.tool_calls else
            ("stop_sequence" if stop_sequence is not None else "end_turn"), "stop_sequence": stop_sequence,
            "usage": {"input_tokens": usage.get("input_tokens", 0),
                      "output_tokens": (usage.get("output_tokens", 0) or 0) + (usage.get("thinking_tokens", 0) or 0),
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}


def message_events(message):
    for index, block in enumerate(message["content"]):
        tool = block["type"] == "tool_use"
        yield {"type": "content_block_start", "index": index,
               "content_block": dict(block, input={}) if tool else dict(block, text="")}
        delta = ({"type": "input_json_delta", "partial_json": json.dumps_text(block["input"])}
                 if tool else {"type": "text_delta", "text": block["text"]})
        yield {"type": "content_block_delta", "index": index, "delta": delta}
        yield {"type": "content_block_stop", "index": index}
    yield {"type": "message_delta", "delta": {"stop_reason": message["stop_reason"],
                                               "stop_sequence": message["stop_sequence"]}, "usage": message["usage"]}
    yield {"type": "message_stop"}


@router.post("/v1/messages")
async def messages(request: Request):
    request_id = "req_" + uuid.uuid4().hex
    if not is_authorized(request.app.state.cfg, request):
        return error_response(401, "Invalid API key", request_id)
    try:
        body = await _read_json(request)
        if body is None:
            raise invalid("Request body must be a JSON object")
        chat = translate(body)
        prepared = await prepare(request, chat)
    except AgyError as exc:
        return error_response(exc.status_code, str(exc), request_id)
    message_id = "msg_" + uuid.uuid4().hex

    def finish(result):
        return message_result(result, body["model"], message_id, body.get("stop_sequences", []))

    if not chat["stream"]:
        try:
            return JSONResponse(finish(await prepared.collect()), headers={"request-id": request_id,
                                "X-Agy-Profile": prepared.profile.summary()})
        except AgyError as exc:
            return error_response(exc.status_code, str(exc), request_id)

    async def stream():
        yield encode({"type": "message_start", "message": {
            "id": message_id, "type": "message", "role": "assistant", "model": body["model"],
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})
        try:
            async with aclosing(prepared.waiting()) as pending:
                async for result in pending:
                    if result is None:
                        yield encode({"type": "ping"})
                    else:
                        for event in message_events(finish(result)):
                            yield encode(event)
        except AgyError as exc:
            error = json.loads(error_response(exc.status_code, str(exc), request_id).body)
            yield encode(error)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"request-id": request_id,
                             "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    request_id = "req_" + uuid.uuid4().hex
    if not is_authorized(request.app.state.cfg, request):
        return error_response(401, "Invalid API key", request_id)
    try:
        body = await _read_json(request)
        if body is None:
            raise invalid("Request body must be a JSON object")
        chat = translate(body, count_only=True)
        images = await ImageInput.prepare(chat, request.app.state.cfg.max_prompt_chars)
        serialized = json.dumps_text({"messages": images.messages, "tools": chat["tools"]})
        # agy exposes usage only after generation, not a tokenizer. Mark this
        # explicitly as an estimate; never use it for accounting or quota.
        estimated = max(1, math.ceil(sum(1 if ord(c) < 128 else 3 for c in serialized) / 3))
        # Approximate pixels, not the length of base64 encoding. The upstream
        # CLI's real image/token accounting is only available after generation.
        estimated += sum(math.ceil(image.pixels / 750) for image in images.attachments)
        return JSONResponse({"input_tokens": estimated}, headers={"request-id": request_id,
                            "X-Agy-Token-Count": "estimate"})
    except AgyError as exc:
        return error_response(exc.status_code, str(exc), request_id)
