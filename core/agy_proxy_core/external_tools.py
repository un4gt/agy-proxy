"""Client-tool request validation shared by the native bridge.

The bridge forwards function definitions and the client conversation as
native function calling. This module only normalizes and validates the
incoming OpenAI-style messages and tool contracts; it never executes a tool.
"""

from __future__ import annotations

from . import json_codec as json
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator

from .agy import AgyError
from .contracts import ToolContract, internal_name


def _json(value) -> str:
    return json.dumps_text(value, sort_keys=True)


def _invalid(message: str) -> AgyError:
    return AgyError(message, status_code=400)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(
        isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        for p in value
    ):
        return "".join(p["text"] for p in value)
    raise _invalid("The bridge currently accepts text content only")


def _arguments(value: str) -> dict:
    try:
        args = json.loads(value)
        if isinstance(args, dict):
            return args
    except (ValueError, TypeError):
        pass
    raise _invalid("Tool arguments must be a JSON object")


def _messages(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise _invalid("messages must be a nonempty array")
    result = []
    pending: set[str] = set()
    seen: set[str] = set()
    for message in raw:
        if not isinstance(message, dict):
            raise _invalid("Each message must be an object")
        role = message.get("role")
        if role not in ("system", "developer", "user", "assistant", "tool"):
            raise _invalid("Unsupported message role in external-tool request")
        item = {"role": role, "content": _text(message.get("content"))}
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise _invalid("tool_call_id must match an outstanding assistant tool call")
            pending.remove(call_id)
            item["tool_call_id"] = call_id
        else:
            if pending:
                raise _invalid("Supply a tool result for every outstanding tool call")
            calls = message.get("tool_calls")
            if calls:
                if role != "assistant" or not isinstance(calls, list):
                    raise _invalid("tool_calls must be an array on an assistant message")
                normalized = []
                for call in calls:
                    if not isinstance(call, dict) or call.get("type") != "function":
                        raise _invalid("Only function tool calls are supported")
                    call_id = call.get("id")
                    function = call.get("function")
                    if not isinstance(call_id, str) or not call_id or call_id in seen:
                        raise _invalid("Tool call ids must be nonempty and unique")
                    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                        raise _invalid("Tool call function.name is required")
                    args = _arguments(function.get("arguments"))
                    normalized.append({"id": call_id, "type": "function", "function": {
                        "name": function["name"], "arguments": _json(args),
                    }})
                    pending.add(call_id)
                    seen.add(call_id)
                item["tool_calls"] = normalized
        result.append(item)
    if pending:
        raise _invalid("Supply a tool result for every outstanding tool call")
    if result[-1]["role"] not in ("user", "tool"):
        raise _invalid("The final message must be a user message or a tool result")
    return result


@dataclass
class ExternalTools:
    functions: dict[str, dict]
    validators: dict[str, Draft202012Validator]
    messages: list[dict]
    choice: str | dict
    parallel: bool
    contracts: dict[str, ToolContract] = field(default_factory=dict)

    @classmethod
    def from_body(cls, body: dict) -> ExternalTools | None:
        raw_messages = body.get("messages")
        has_tool_history = isinstance(raw_messages, list) and any(
            isinstance(m, dict) and (m.get("role") == "tool" or m.get("tool_calls"))
            for m in raw_messages
        )
        if "tools" not in body and "tool_choice" not in body and not has_tool_history:
            return None
        raw_tools = body.get("tools", [])
        if not isinstance(raw_tools, list):
            raise _invalid("tools must be an array")
        functions = {}
        validators = {}
        contracts = {}
        aliases = {}
        for tool in raw_tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise _invalid("Only tools of type function are supported")
            function = tool.get("function")
            if not isinstance(function, dict):
                raise _invalid("Tool function definition is required")
            name = function.get("name")
            if not isinstance(name, str) or not name or len(name) > 1024:
                raise _invalid("Invalid tool function name")
            if name in functions:
                raise _invalid("Duplicate tool function name")
            schema = function.get("parameters", {"type": "object", "properties": {}})
            if not isinstance(schema, dict):
                raise _invalid("Tool parameters must be a JSON schema object")
            contract = ToolContract.build(name, schema, original_schema=tool.get("_original_parameters"))
            if contract.alias in aliases:
                raise _invalid("Tool internal-name collision")
            aliases[contract.alias] = name
            contracts[name] = contract
            functions[name] = dict(function, parameters=contract.schema)
            validators[contract.alias] = contract.validator
        choice = body.get("tool_choice", "auto" if functions else "none")
        if isinstance(choice, str):
            if choice not in ("auto", "none", "required"):
                raise _invalid("tool_choice must be auto, none, required, or a named function")
            if choice == "required" and not functions:
                raise _invalid("tool_choice required needs at least one tool")
        elif isinstance(choice, dict):
            fn = choice.get("function")
            if (choice.get("type") != "function" or not isinstance(fn, dict)
                    or not isinstance(fn.get("name"), str) or fn["name"] not in functions):
                raise _invalid("tool_choice must name a declared function")
        else:
            raise _invalid("Invalid tool_choice")
        parallel = body.get("parallel_tool_calls", True)
        if not isinstance(parallel, bool):
            raise _invalid("parallel_tool_calls must be a boolean")
        if body.get("n", 1) != 1:
            raise _invalid("The bridge supports n=1 only")
        response_format = body.get("response_format") or {}
        if not isinstance(response_format, dict) or response_format.get("type", "text") != "text":
            raise _invalid("response_format cannot be combined with tools")
        messages = _messages(raw_messages)
        for message in messages:
            for call in message.get("tool_calls", []):
                name = call["function"]["name"]
                alias = internal_name(name)
                if alias in aliases and aliases[alias] != name:
                    raise _invalid("Tool internal-name collision in history")
                aliases[alias] = name
        return cls(functions, validators, messages, choice, parallel, contracts)
