"""Protocol-independent client tool contracts. Never transform argument data."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from referencing import Registry

from . import json_codec as json
from .agy import AgyError


def schema_nodes(schema):
    """Visit schema positions only; property names and annotation data are opaque."""
    if not isinstance(schema, dict):
        return
    yield schema
    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        for child in (schema.get(key) or {}).values():
            yield from schema_nodes(child)
    for key in ("items", "additionalItems", "additionalProperties", "unevaluatedProperties",
                "unevaluatedItems", "contains", "propertyNames", "not", "if", "then", "else", "contentSchema"):
        child = schema.get(key)
        for item in child if isinstance(child, list) else [child]:
            yield from schema_nodes(item)
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for child in schema.get(key, []):
            yield from schema_nodes(child)
    for child in (schema.get("dependencies") or {}).values():
        yield from schema_nodes(child)


def normalize_schema(schema: dict, *, gemini=False) -> dict:
    result = deepcopy(schema)
    try:
        for node in schema_nodes(result):
            for key in ("$ref", "$dynamicRef"):
                if key in node and (not isinstance(node[key], str) or not node[key].startswith("#")):
                    raise AgyError("JSON schemas may only use local references", status_code=400)
            if gemini:
                kind = node.get("type")
                if isinstance(kind, str):
                    node["type"] = kind.lower()
                nullable = node.pop("nullable", False)
                if not isinstance(nullable, bool):
                    raise AgyError("Schema nullable must be a boolean", status_code=400)
                if nullable:
                    original = dict(node)
                    node.clear()
                    node["anyOf"] = [original, {"type": "null"}]
        validator = validator_for(result, default=Draft202012Validator if "$schema" not in result else None)
        if validator is None:
            raise AgyError("Unsupported JSON Schema dialect", status_code=400)
        validator.check_schema(result)
    except (SchemaError, AttributeError, TypeError):
        raise AgyError("Invalid JSON parameter schema", status_code=400) from None
    return result


def internal_name(name: str) -> str:
    if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        return name
    return "tool_" + hashlib.sha256(name.encode()).hexdigest()[:48]


@dataclass
class ToolContract:
    client_name: str
    alias: str
    original_schema: dict
    schema: dict
    validator: Draft202012Validator
    fingerprint: str

    @classmethod
    def build(cls, name, schema, *, original_schema=None):
        normalized = normalize_schema(schema)
        fingerprint = hashlib.sha256(json.dumps_text(normalized, sort_keys=True).encode()).hexdigest()
        return cls(name, internal_name(name), deepcopy(schema if original_schema is None else original_schema), normalized,
                   validator_for(normalized)(normalized, registry=Registry()), fingerprint)


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = ""


@dataclass
class ExecutionOutcome:
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    conversation_id: str | None = None
    num_turns: int | None = None
    diagnostics: dict = field(default_factory=dict)


@dataclass
class CanonicalRequest:
    """The executor sees contracts/callbacks, never HTTP protocol fields."""
    messages: list
    configuration: object
    output_schema: dict
    prompt: Callable
    validate: Callable
    confirmed_reply: Callable
    tools: dict[str, ToolContract] = field(default_factory=dict)
    session_instructions: str = ""
    attachments: tuple = ()
    clean_result: Callable | None = None
    model: str | None = None
    effort: str | None = None


class CandidateError(AgyError):
    def __init__(self, message, *, code, feedback=None):
        super().__init__(message, code=code)
        self.feedback = feedback or {"constraint": code}


def validation_feedback(error, *, tool=None):
    missing = []
    if error.validator == "required" and isinstance(error.instance, dict):
        missing = [key for key in error.validator_value if key not in error.instance]
    expectation = {}
    if error.validator in ("type", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                           "minLength", "maxLength", "minItems", "maxItems", "multipleOf", "uniqueItems"):
        expectation["expected"] = error.validator_value
    return {"tool": tool, "path": "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1")
                                                    for p in error.absolute_path),
            "constraint": error.validator, "missing": missing, **expectation}
