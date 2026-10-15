"""Gemini function histories and schemas at the protocol boundary."""
from __future__ import annotations

from . import json_codec as json
from .contracts import normalize_schema
from .protocol_common import array, obj, string, invalid, optional_obj


def translate(body, *, model, stream=False):
    if body.get("cachedContent") or body.get("cached_content"):
        raise invalid("Gemini cachedContent resources are not supported")
    tools = []
    for group in array(body.get("tools", []), "tools"):
        group = obj(group, "tool")
        if set(group) - {"functionDeclarations", "function_declarations"}:
            raise invalid("Only client functionDeclarations are supported; built-in server tools are unavailable")
        for fn in array(group.get("functionDeclarations", group.get("function_declarations", [])), "functionDeclarations"):
            fn = obj(fn, "function declaration")
            if fn.get("behavior") not in (None, "BLOCKING"):
                raise invalid("Non-blocking Gemini functions are not supported")
            original = fn.get("parametersJsonSchema", fn.get("parameters", {"type": "object"}))
            schema = normalize_schema(obj(original, "parameters"), gemini="parametersJsonSchema" not in fn)
            tools.append({"type": "function", "_original_parameters": original, "function": {
                "name": string(fn.get("name"), "function.name", nonempty=True),
                "description": string(fn.get("description", ""), "function.description"), "parameters": schema}})
    config = optional_obj(body, "toolConfig") or optional_obj(body, "tool_config")
    if set(config) - {"functionCallingConfig", "function_calling_config"}:
        raise invalid("Unsupported Gemini toolConfig")
    calling = obj(config.get("functionCallingConfig", config.get("function_calling_config", {})), "functionCallingConfig")
    mode = calling.get("mode", "AUTO")
    if mode not in ("AUTO", "NONE", "ANY", "VALIDATED", "MODE_UNSPECIFIED"):
        raise invalid("Unsupported function calling mode")
    choice = {"NONE": "none", "ANY": "required"}.get(mode, "auto" if tools else "none")
    allowed = calling.get("allowedFunctionNames", calling.get("allowed_function_names"))
    if allowed is not None:
        allowed = array(allowed, "allowedFunctionNames")
        names = {t["function"]["name"] for t in tools}
        if mode != "ANY" or not allowed or any(not isinstance(n, str) or n not in names for n in allowed):
            raise invalid("allowedFunctionNames must name declared functions in ANY mode")
        tools = [t for t in tools if t["function"]["name"] in allowed]
        if len(allowed) == 1:
            choice = {"type": "function", "function": {"name": allowed[0]}}
    messages = []
    system = body.get("systemInstruction", body.get("system_instruction"))
    if system is not None:
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        else:
            parts = array(obj(system, "systemInstruction").get("parts"), "systemInstruction.parts")
            messages.append({"role": "system", "content": "".join(string(obj(p, "part").get("text"), "text") for p in parts)})
    pending = {}
    seen = set()
    for index, content in enumerate(array(body.get("contents"), "contents")):
        content = obj(content, "content")
        role = content.get("role", "user")
        if role not in ("user", "model", "function", "tool"):
            raise invalid("Unsupported Gemini content role")
        parts, calls, results = [], [], []
        for pi, raw in enumerate(array(content.get("parts"), "parts")):
            part = obj(raw, "part")
            keys = [k for k in ("text", "inlineData", "inline_data", "fileData", "file_data",
                                "functionCall", "function_call", "functionResponse", "function_response") if k in part]
            if len(keys) != 1:
                raise invalid("Unsupported or ambiguous Gemini part")
            key = keys[0]
            if part.get("thought"):
                continue
            if key == "text":
                parts.append({"type": "text", "text": string(part[key], "text")})
            elif key in ("inlineData", "inline_data", "fileData", "file_data"):
                data = obj(part[key], key)
                mime = string(data.get("mimeType", data.get("mime_type")), "mimeType")
                if not mime.startswith("image/"):
                    raise invalid("Only image attachments are supported")
                url = ("data:" + mime + ";base64," + string(data.get("data"), "data")
                       if key in ("inlineData", "inline_data") else
                       string(data.get("fileUri", data.get("file_uri")), "fileUri"))
                parts.append({"type": "image_url", "image_url": {"url": url}})
            elif key in ("functionCall", "function_call"):
                if role != "model":
                    raise invalid("functionCall requires the model role")
                fn = obj(part[key], key)
                name = string(fn.get("name"), "functionCall.name", nonempty=True)
                call_id = string(fn.get("id", f"gemini_history_{index}_{pi}"), "functionCall.id", nonempty=True)
                if call_id in seen:
                    raise invalid("Duplicate functionCall id")
                seen.add(call_id)
                pending[call_id] = name
                calls.append({"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": json.dumps_text(obj(fn.get("args", {}), "functionCall.args"))}})
            else:
                if role == "model":
                    raise invalid("functionResponse cannot use the model role")
                fn = obj(part[key], key)
                name = string(fn.get("name"), "functionResponse.name", nonempty=True)
                call_id = fn.get("id")
                if call_id is None:
                    matches = [cid for cid, n in pending.items() if n == name]
                    if len(matches) != 1:
                        raise invalid("functionResponse without id must uniquely match an outstanding function name")
                    call_id = matches[0]
                if not isinstance(call_id, str) or pending.get(call_id) != name:
                    raise invalid("functionResponse id/name does not match an outstanding call")
                del pending[call_id]
                if fn.get("scheduling") is not None or fn.get("willContinue"):
                    raise invalid("Streaming function responses are not supported")
                results.append({"role": "tool", "tool_call_id": call_id,
                                "content": json.dumps_text(obj(fn.get("response"), "functionResponse.response"))})
        messages.extend(results)
        if parts or calls:
            message = {"role": "assistant" if role == "model" else "user", "content": parts}
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
    if pending:
        raise invalid("Supply a functionResponse for every outstanding functionCall")
    gen = optional_obj(body, "generationConfig") or optional_obj(body, "generation_config")
    if gen.get("candidateCount", 1) != 1:
        raise invalid("Only candidateCount=1 is supported")
    if gen.get("responseModalities", ["TEXT"]) != ["TEXT"]:
        raise invalid("Only TEXT output is supported")
    thinking = obj(gen.get("thinkingConfig", gen.get("thinking_config", {})), "thinkingConfig")
    effort = thinking.get("thinkingLevel", thinking.get("thinking_level"))
    chat = {"model": model, "stream": stream, "messages": messages, "tools": tools,
            "tool_choice": choice, "reasoning_effort": effort.lower() if isinstance(effort, str) else effort}
    mime = gen.get("responseMimeType", "text/plain")
    if mime == "application/json":
        schema = gen.get("responseJsonSchema", gen.get("responseSchema"))
        chat["response_format"] = ({"type": "json_object"} if schema is None else {
            "type": "json_schema", "json_schema": {"name": "gemini_output", "schema": normalize_schema(
                obj(schema, "responseSchema"), gemini="responseJsonSchema" not in gen)}})
    elif mime != "text/plain":
        raise invalid("Unsupported responseMimeType")
    return chat


def result_body(result, *, model, response_id):
    from .gemini_api import _usage_meta
    parts = [{"text": result.text}] if result.text else []
    parts += [{"functionCall": {"id": call.id, "name": call.name, "args": call.arguments}} for call in result.calls]
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "index": 0, "finishReason": "STOP"}],
            "usageMetadata": _usage_meta(result.usage), "modelVersion": model or "agy-default", "responseId": response_id}


def result_events(result, **context):
    yield result_body(result, **context)
