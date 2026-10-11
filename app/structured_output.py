"""Validated OpenAI response formats backed by agy's structured output mode."""

from __future__ import annotations

from . import json_codec as json
import re
from contextlib import aclosing
from dataclasses import dataclass
from typing import AsyncIterator

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry

from .agy import AgyError, SessionManager
from .runner import TurnEvent, TurnProfile, TurnResult, build_seed_prompt
from .session_context import SessionContext, SessionIdentity


def local_refs_only(value) -> None:
    """Never let a client schema trigger a network or filesystem lookup."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("$ref", "$dynamicRef") and (
                not isinstance(child, str) or not child.startswith("#")
            ):
                raise AgyError("JSON schemas may only use local references", status_code=400)
            local_refs_only(child)
    elif isinstance(value, list):
        for child in value:
            local_refs_only(child)


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
        local_refs_only(schema)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise AgyError("Invalid response_format JSON schema", status_code=400) from exc
        return cls(schema, Draft202012Validator(schema, registry=Registry()), kind == "json_object")

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
        except Exception as exc:
            # Never expose invalid JSON as a successful completion, including SSE.
            raise AgyError("agy returned output that does not match response_format") from exc
        return TurnResult(text=text, usage=envelope.get("usage") or {},
                          conversation_id=envelope.get("conversation_id"),
                          num_turns=envelope.get("num_turns"))

    async def run(self, mgr: SessionManager, *, system: str, history: list,
                  final_user: str | None, model: str | None, effort: str | None,
                  profile: TurnProfile, images=None, session_key: SessionIdentity | str | None = None) -> AsyncIterator[TurnEvent]:
        profile.model = model
        profile.mark("turn_start")
        messages = [(r, t.strip() if r == "assistant" else t) for r, t in history]
        if final_user is not None:
            messages.append(("user", final_user))
        context = SessionContext.build(session_key, ["structured", system, self.output_schema], messages)
        session = await mgr.acquire(model=model, effort=effort, output_schema=self.output_schema,
                                    attachments=images.attachments if images else (), context=context)
        profile.new_session = session.turns == 0
        profile.mark("session_ready")
        instruction = (
            "Generate the JSON object requested by the client, including every requested field. "
            "Use finish with its json field set to the serialized JSON object, without Markdown."
            if self.json_mode else
            "Return a JSON object matching the supplied response schema. "
            "Use finish to submit the object itself, without Markdown or commentary."
        )
        prompt = instruction + "\n\n" + (build_seed_prompt("", messages[session.input_offset:], None)
                                             if session.input_offset else build_seed_prompt(system, history, final_user))
        if images:
            prompt += images.instruction(session)
        result = None
        complete = False
        try:
            yield "ready", None
            async with aclosing(session.send_turn(prompt)) as events:
                async for event in events:
                    if not isinstance(event, dict):
                        continue
                    if event.get("event") == "init":
                        profile.mark("init_seen")
                        if (event.get("init") or {}).get("agent") != "proxy-external":
                            raise AgyError("agy did not select the structured-output agent; upgrade the CLI")
                    if event.get("event") == "step_update":
                        step = event.get("step_update") or {}
                        session.check_tool_step(step)
                    if event.get("event") == "result":
                        envelope = event.get("result") or {}
                        if envelope.get("status") != "SUCCESS":
                            raise AgyError(envelope.get("error") or "agy structured-output turn failed")
                        session.check_images_seen()
                        result = self.result(envelope)
                        if images:
                            images.check_structured_result(result, session)
                        profile.mark("result")
            if result is None:
                raise AgyError("agy ended without a structured response")
            # Buffer and validate first so no invalid output can escape in a delta.
            profile.mark("first_delta")
            yield "delta", result.text
            complete = True
            yield "result", result
        finally:
            await mgr.release(session, reply=("assistant", result.text.strip()) if complete else None)
