"""Validated OpenAI response formats backed by agy's structured output mode."""

from __future__ import annotations

from . import json_codec as json
import re
from contextlib import aclosing
from dataclasses import dataclass
from typing import AsyncIterator

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for
from referencing import Registry

from .agy import AgyError, SessionManager
from .contracts import CanonicalRequest, CandidateError, normalize_schema, validation_feedback
from .execution import execute
from .runner import TurnEvent, TurnProfile, TurnResult, build_seed_prompt
from .session_context import SessionIdentity


@dataclass
class ResponseFormat:
    schema: dict
    validator: Draft202012Validator
    json_mode: bool = False

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
        # agy's finish tool treats a schema without named properties as an
        # empty argument object. A string envelope preserves arbitrary client
        # fields; validate and unwrap it before exposing anything to the client.
        return {"type": "object", "properties": {"json": {
            "type": "string",
            "description": "The complete JSON object requested by the client, serialized as JSON text. Include all requested fields.",
        }}, "required": ["json"], "additionalProperties": False}

    def result(self, envelope: dict) -> TurnResult:
        try:
            output = envelope.get("structured_output")
            if output is None:
                output = json.loads(envelope.get("response", ""))
            if self.json_mode:
                Draft202012Validator(self.output_schema).validate(output)
                output = json.loads(output["json"])
            self.validator.validate(output)
            text = json.dumps_text(output, strict=True)
        except ValidationError as exc:
            raise CandidateError("agy returned output that does not match response_format",
                                 code="structured_output_invalid", feedback=validation_feedback(exc)) from None
        except (ValueError, TypeError, KeyError):
            raise CandidateError("agy did not return valid structured JSON", code="output_json_invalid") from None
        except Exception:
            raise AgyError("Cannot resolve response_format schema", status_code=400, code="invalid_output_schema") from None
        return TurnResult(text=text, usage=envelope.get("usage") or {},
                          conversation_id=envelope.get("conversation_id"),
                          num_turns=envelope.get("num_turns"))

    async def run(self, mgr: SessionManager, *, system: str, history: list,
                  final_user: str | None, model: str | None, effort: str | None,
                  profile: TurnProfile, images=None, session_key: SessionIdentity | str | None = None) -> AsyncIterator[TurnEvent]:
        messages = [(r, t.strip() if r == "assistant" else t) for r, t in history]
        if final_user is not None:
            messages.append(("user", final_user))
        instruction = (
            "Generate the JSON object requested by the client, including every requested field. "
            "Use finish with its json field set to the serialized JSON object, without Markdown."
            if self.json_mode else
            "Return a JSON object matching the supplied response schema. "
            "Use finish to submit the object itself, without Markdown or commentary."
        )
        def prompt(session):
            text = instruction + "\n\n" + (build_seed_prompt("", messages[session.input_offset:], None)
                                                if session.input_offset else build_seed_prompt(system, history, final_user))
            return text + (images.instruction(session) if images else "")
        request = CanonicalRequest(
            messages=messages, configuration=["structured", system, self.output_schema],
            output_schema=self.output_schema, prompt=prompt, validate=self.result,
            confirmed_reply=lambda result: ("assistant", result.text.strip()),
            attachments=images.attachments if images else (),
            clean_result=images.check_structured_result if images else None, model=model, effort=effort)
        async with aclosing(execute(request, mgr, profile=profile, session_key=session_key)) as events:
            async for event in events:
                yield event
