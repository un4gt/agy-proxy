"""Validated client response formats for the native bridge.

The bridge submits structured output through a synthetic tool and validates
the returned JSON against the client's schema before exposing it.
"""

from __future__ import annotations

import re

from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for
from referencing import Registry

from .agy import AgyError
from .contracts import normalize_schema


class ResponseFormat:
    def __init__(self, schema: dict, validator: Draft202012Validator, json_mode: bool = False):
        self.schema = schema
        self.validator = validator
        self.json_mode = json_mode

    @classmethod
    def from_body(cls, body: dict) -> ResponseFormat | None:
        raw = body.get("response_format")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise AgyError("response_format must be an object", status_code=400)
        kind = raw.get("type")
        if kind == "text":
            return None
        if kind == "json_object":
            schema = {"type": "object"}
        elif kind == "json_schema":
            spec = raw.get("json_schema")
            if not isinstance(spec, dict):
                raise AgyError("response_format.json_schema is required", status_code=400)
            name = spec.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
                raise AgyError("Invalid response_format.json_schema.name", status_code=400)
            if spec.get("strict") is not None and not isinstance(spec["strict"], bool):
                raise AgyError("json_schema.strict must be a boolean", status_code=400)
            schema = spec.get("schema")
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise AgyError("json_schema.schema must describe an object", status_code=400)
        else:
            raise AgyError("response_format.type must be text, json_object, or json_schema", status_code=400)
        schema = normalize_schema(schema)
        return cls(schema, validator_for(schema)(schema, registry=Registry()), kind == "json_object")

    @property
    def output_schema(self) -> dict:
        if not self.json_mode:
            return self.schema
        # The bridge treats a schema without named properties as an empty
        # argument object. A string envelope preserves arbitrary client fields;
        # validate and unwrap it before exposing anything to the client.
        return {"type": "object", "properties": {"json": {
            "type": "string",
            "description": "The complete JSON object requested by the client, serialized as JSON text. Include all requested fields.",
        }}, "required": ["json"], "additionalProperties": False}
