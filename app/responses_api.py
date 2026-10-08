"""Text/function/custom-tool Responses API, including typed SSE and bounded state."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import OrderedDict
from contextlib import aclosing
from copy import deepcopy

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .agy import AgyError
from .auth import extract_key, is_authorized, openai_error
from .openai_api import _err_type, _read_json
from .protocol_common import array, boolean, encode, invalid, obj, optional_obj, prepare, string, text_content

router = APIRouter()


class ResponseCache:
    """Process-local state; explicit IDs only, with credential ownership and TTL."""
    def __init__(self, *, ttl=3600, max_bytes=32 * 1024 * 1024, max_entries=256):
        self.ttl, self.max_bytes, self.max_entries = ttl, max_bytes, max_entries
        self.entries = OrderedDict()
        self.size = 0

    def prune(self):
        now = time.monotonic()
        for key, (expiry, _, _, size) in list(self.entries.items()):
            if expiry <= now:
                self.size -= size
                del self.entries[key]

    def put(self, owner, response, messages):
        self.prune()
        size = len(json.dumps([response, messages], ensure_ascii=False).encode())
        if size > self.max_bytes:
            return
        while self.entries and (len(self.entries) >= self.max_entries or self.size + size > self.max_bytes):
            _, entry = self.entries.popitem(last=False)
            self.size -= entry[3]
        self.entries[(owner, response["id"])] = (time.monotonic() + self.ttl,
                                                deepcopy(response), deepcopy(messages), size)
        self.size += size

    def get(self, owner, response_id):
        self.prune()
        entry = self.entries.get((owner, response_id))
        if entry is None:
            raise AgyError("Response not found, expired, or not stored", status_code=404)
        return deepcopy(entry[1]), deepcopy(entry[2])

    def delete(self, owner, response_id):
        self.get(owner, response_id)
        self.size -= self.entries.pop((owner, response_id))[3]


def cache_for(request):
    if not hasattr(request.app.state, "responses_cache"):
        request.app.state.responses_cache = ResponseCache()
    owner = hashlib.sha256((extract_key(request) or "anonymous").encode()).hexdigest()
    return request.app.state.responses_cache, owner


def tool_alias(name, namespace=None):
    name = string(name, "tool.name", nonempty=True)
    if namespace:
        string(namespace, "tool.namespace", nonempty=True)
        return "ns_" + hashlib.sha256((namespace + "." + name).encode()).hexdigest()[:40]
    return name


def convert_tools(raw):
    tools, specs = [], {}

    def add(tool, namespace=None):
        tool = obj(tool, "tool")
        kind = tool.get("type")
        name = string(tool.get("name"), "tool.name", nonempty=True)
        if kind == "namespace" and namespace is None:
            for child in array(tool.get("tools"), "namespace.tools"):
                add(child, name)
            return
        if kind not in ("function", "custom"):
            raise invalid(f"Unsupported Responses tool type: {kind}")
        alias = tool_alias(name, namespace)
        if alias in specs:
            raise invalid("Duplicate tool name")
        specs[alias] = {"name": name, "type": kind, **({"namespace": namespace} if namespace else {})}
        description = string(tool.get("description", ""), "tool.description")
        if kind == "custom":
            fmt = obj(tool.get("format", {"type": "text"}), "custom.format")
            if fmt.get("type") not in ("text", "grammar"):
                raise invalid("Unsupported custom tool format")
            description += "\nReturn the raw tool input in the input string, without JSON or Markdown wrappers."
            if fmt.get("type") == "grammar":
                description += "\nThe input must follow this grammar: " + json.dumps(fmt.get("grammar"))
            parameters = {"type": "object", "properties": {"input": {"type": "string"}},
                          "required": ["input"], "additionalProperties": False}
        else:
            parameters = tool.get("parameters")
            if parameters is None:
                parameters = {"type": "object", "properties": {}}
        tools.append({"type": "function", "function": {
            "name": alias, "description": description, "parameters": parameters}})

    for tool in array(raw, "tools"):
        add(tool)
    return tools, specs


def convert_input(raw):
    if isinstance(raw, str):
        return [{"role": "user", "content": raw}]
    messages = []
    for item in array(raw, "input"):
        item = obj(item, "input item")
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role")
            if role not in ("system", "developer", "user", "assistant"):
                raise invalid("Unsupported input message role")
            messages.append({"role": role, "content": text_content(item.get("content"))})
        elif kind in ("function_call", "custom_tool_call"):
            name = tool_alias(item.get("name"), item.get("namespace"))
            args = (json.dumps({"input": string(item.get("input"), "custom_tool_call.input")})
                    if kind == "custom_tool_call" else string(item.get("arguments"), "arguments"))
            call = {"id": string(item.get("call_id"), "call_id", nonempty=True), "type": "function",
                    "function": {"name": name, "arguments": args}}
            if not messages or messages[-1]["role"] != "assistant":
                messages.append({"role": "assistant", "content": ""})
            messages[-1].setdefault("tool_calls", []).append(call)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            messages.append({"role": "tool", "tool_call_id": string(item.get("call_id"), "call_id"),
                             "content": text_content(item.get("output"))})
        elif kind == "reasoning":
            # Reasoning metadata is not an executable turn or user instruction.
            # There is no provider-specific encrypted reasoning state upstream.
            if item.get("encrypted_content"):
                raise invalid("Encrypted reasoning from another provider cannot be replayed")
        else:
            raise invalid(f"Unsupported Responses input item: {kind}")
    return messages


def translate(body, previous):
    tools, specs = convert_tools(body.get("tools", []))
    messages = previous + convert_input(body.get("input", []))
    instructions = body.get("instructions")
    if instructions is not None:
        messages = [{"role": "system", "content": string(instructions, "instructions")}] + messages
    choice = body.get("tool_choice", "auto" if tools else "none")
    if isinstance(choice, dict):
        if choice.get("type") in ("function", "custom"):
            choice = {"type": "function", "function": {
                "name": tool_alias(choice.get("name"), choice.get("namespace"))}}
        else:
            raise invalid("Unsupported Responses tool_choice")
    reasoning = optional_obj(body, "reasoning")
    chat = {"model": body.get("model"), "messages": messages, "tools": tools,
            "tool_choice": choice, "stream": boolean(body, "stream"),
            "reasoning_effort": reasoning.get("effort"),
            "parallel_tool_calls": boolean(body, "parallel_tool_calls", True)}
    text = optional_obj(body, "text")
    fmt = obj(text.get("format", {"type": "text"}), "text.format")
    if fmt.get("type") == "json_schema":
        chat["response_format"] = {"type": "json_schema", "json_schema": {
            k: v for k, v in fmt.items() if k != "type"}}
    else:
        chat["response_format"] = fmt
    return chat, specs


def output_items(result, specs):
    output = []
    if result.text:
        output.append({"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
                       "status": "completed", "content": [{"type": "output_text", "text": result.text,
                                                            "annotations": [], "logprobs": []}]})
    for call in result.tool_calls:
        fn = call["function"]
        spec = specs[fn["name"]]
        custom = spec["type"] == "custom"
        item = {"id": ("ct_" if custom else "fc_") + uuid.uuid4().hex,
                "type": "custom_tool_call" if custom else "function_call",
                "status": "completed", "call_id": call["id"], "name": spec["name"]}
        if "namespace" in spec:
            item["namespace"] = spec["namespace"]
        item.update({"input": json.loads(fn["arguments"])["input"]} if custom else {"arguments": fn["arguments"]})
        output.append(item)
    return output


def completed_events(response):
    for index, item in enumerate(response["output"]):
        start = dict(item, status="in_progress")
        kind = item["type"]
        if kind == "message":
            start["content"] = []
        else:
            start["input" if kind == "custom_tool_call" else "arguments"] = ""
        yield {"type": "response.output_item.added", "output_index": index, "item": start}
        common = {"item_id": item["id"], "output_index": index}
        if kind == "message":
            for ci, part in enumerate(item["content"]):
                attrs = dict(common, content_index=ci)
                yield {"type": "response.content_part.added", **attrs, "part": dict(part, text="")}
                yield {"type": "response.output_text.delta", **attrs, "delta": part["text"], "logprobs": []}
                yield {"type": "response.output_text.done", **attrs, "text": part["text"], "logprobs": []}
                yield {"type": "response.content_part.done", **attrs, "part": part}
        else:
            field = "input" if kind == "custom_tool_call" else "arguments"
            event = "custom_tool_call_input" if field == "input" else "function_call_arguments"
            yield {"type": f"response.{event}.delta", **common, "delta": item[field]}
            yield {"type": f"response.{event}.done", **common, field: item[field]}
        yield {"type": "response.output_item.done", "output_index": index, "item": item}
    yield {"type": "response.completed", "response": response}


@router.post("/v1/responses")
async def responses(request: Request):
    if not is_authorized(request.app.state.cfg, request):
        return openai_error(401, "Invalid API key", "authentication_error", "invalid_api_key")
    try:
        body = await _read_json(request)
        if body is None:
            raise invalid("Request body must be a JSON object")
        if len(json.dumps(body, ensure_ascii=False)) > request.app.state.cfg.max_prompt_chars:
            raise AgyError("Request too large", status_code=413)
        if boolean(body, "background") or body.get("conversation"):
            raise invalid("background and conversation resources are not supported; use previous_response_id")
        if body.get("truncation", "disabled") != "disabled":
            raise invalid("Automatic truncation is not supported")
        limit = body.get("max_output_tokens")
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise invalid("max_output_tokens must be a positive integer")
        store = boolean(body, "store", True)
        cache, owner = cache_for(request)
        previous = []
        if body.get("previous_response_id") is not None:
            _, previous = cache.get(owner, string(body["previous_response_id"], "previous_response_id"))
        chat, specs = translate(body, previous)
        prepared = await prepare(request, chat)
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))

    response = {"id": "resp_" + uuid.uuid4().hex, "object": "response", "created_at": int(time.time()),
                "status": "in_progress", "error": None, "incomplete_details": None,
                "model": body["model"], "output": [], "usage": None,
                "parallel_tool_calls": chat["parallel_tool_calls"], "tools": body.get("tools", []),
                "tool_choice": body.get("tool_choice", "auto"), "store": store,
                "previous_response_id": body.get("previous_response_id"), "instructions": body.get("instructions"),
                "reasoning": body.get("reasoning"), "text": body.get("text", {"format": {"type": "text"}}),
                "metadata": body.get("metadata", {}), "max_output_tokens": limit}

    def finish(result):
        usage = result.usage
        inp, out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        thinking = usage.get("thinking_tokens", 0) or 0
        final = dict(response, status="completed", completed_at=int(time.time()), output=output_items(result, specs),
                     usage={"input_tokens": inp, "output_tokens": out + thinking,
                            "total_tokens": inp + out + thinking,
                            "input_tokens_details": {"cached_tokens": usage.get("cached_input_tokens", 0)},
                            "output_tokens_details": {"reasoning_tokens": thinking}})
        if store:
            # Request instructions do not carry to the next response. Input system
            # messages do, and each branch gets a deep copy, never shared mutation.
            history = previous + convert_input(body.get("input", [])) + [result.message()]
            cache.put(owner, final, history)
        return final

    if not chat["stream"]:
        try:
            return JSONResponse(finish(await prepared.collect()), headers={"X-Agy-Profile": prepared.profile.summary()})
        except AgyError as exc:
            return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))

    async def stream():
        sequence = 0

        def event(value):
            nonlocal sequence
            data = encode(dict(value, sequence_number=sequence))
            sequence += 1
            return data

        yield event({"type": "response.created", "response": response})
        yield event({"type": "response.in_progress", "response": response})
        try:
            async with aclosing(prepared.waiting()) as pending:
                async for result in pending:
                    if result is None:
                        yield b": keep-alive\n\n"
                    else:
                        for value in completed_events(finish(result)):
                            yield event(value)
        except AgyError as exc:
            error = {"code": _err_type(exc.status_code), "message": str(exc)}
            yield event({"type": "response.failed", "response": dict(response, status="failed", error=error)})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/v1/responses/{response_id}")
async def retrieve_response(response_id: str, request: Request):
    if not is_authorized(request.app.state.cfg, request):
        return openai_error(401, "Invalid API key", "authentication_error")
    try:
        cache, owner = cache_for(request)
        response, _ = cache.get(owner, response_id)
        return JSONResponse(response)
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))


@router.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str, request: Request):
    if not is_authorized(request.app.state.cfg, request):
        return openai_error(401, "Invalid API key", "authentication_error")
    try:
        cache, owner = cache_for(request)
        cache.delete(owner, response_id)
        return JSONResponse({"id": response_id, "object": "response.deleted", "deleted": True})
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))
