"""Shared execution for the Responses and Messages wire adapters.

All tools run on the client. Every turn retains the existing private process,
workspace, schema validation, cancellation cleanup and usage accounting.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from anyio import CancelScope
from fastapi import Request

from .agy import AgyError, list_models
from .external_tools import ExternalTools
from .openai_api import _parse_messages, _resolve_model
from .reasoning import apply_effort, parse_effort
from .runner import TurnProfile, TurnResult, record_dashboard_usage
from .structured_output import ResponseFormat


def invalid(message: str) -> AgyError:
    return AgyError(message, status_code=400)


def obj(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise invalid(f"{field} must be an object")
    return value


def optional_obj(body: dict, field: str) -> dict:
    value = body.get(field)
    return {} if value is None else obj(value, field)


def array(value, field: str) -> list:
    if not isinstance(value, list):
        raise invalid(f"{field} must be an array")
    return value


def string(value, field: str, *, nonempty=False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise invalid(f"{field} must be a {'nonempty ' if nonempty else ''}string")
    return value


def boolean(body: dict, field: str, default=False) -> bool:
    value = body.get(field, default)
    if not isinstance(value, bool):
        raise invalid(f"{field} must be a boolean")
    return value


def text_content(value, *, types=("text", "input_text", "output_text")) -> str:
    if isinstance(value, str):
        return value
    parts = []
    for part in array(value, "content"):
        part = obj(part, "content block")
        if part.get("type") not in types:
            raise invalid(f"Unsupported content type: {part.get('type')}; this proxy accepts text only")
        parts.append(string(part.get("text"), "content.text"))
    return "".join(parts)


def encode(event: dict) -> bytes:
    return ("event: " + event["type"] + "\ndata: " +
            json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n\n").encode()


@dataclass
class PreparedTurn:
    request: Request
    body: dict
    model: str | None
    profile: TurnProfile
    turn: object

    async def collect(self) -> TurnResult:
        started = time.monotonic()
        result = None
        status = "ABORTED"
        try:
            async for kind, value in self.turn:
                if kind == "result":
                    result = value
            if result is None:
                raise AgyError("Upstream ended without a response")
            status = "SUCCESS"
            return result
        except AgyError as exc:
            status = f"ERROR {exc.status_code}"
            raise
        finally:
            with CancelScope(shield=True):
                await self.turn.aclose()
                await record_dashboard_usage(
                    self.request.app.state.cfg, self.request.app.state,
                    model=self.model, request=self.request,
                    usage=result.usage if result else None,
                    duration_ms=(time.monotonic() - started) * 1000, status=status,
                )

    async def waiting(self):
        """Keep the HTTP stream alive while the validated tool result is built."""
        task = asyncio.create_task(self.collect())
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=10)
                if not done:
                    yield None
            yield task.result()
        finally:
            with CancelScope(shield=True):
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def prepare(request: Request, body: dict) -> PreparedTurn:
    cfg = request.app.state.cfg
    if len(json.dumps(body, ensure_ascii=False)) > cfg.max_prompt_chars:
        raise AgyError("Prompt and tool definitions too long for this proxy", status_code=413)
    requested = string(body.get("model"), "model", nonempty=True)
    boolean(body, "stream")
    effort = parse_effort(body.get("reasoning_effort"))
    if effort is None and cfg.effort:
        effort = parse_effort(cfg.effort)
    # Validate before looking up models or leasing any process.
    response_format = ResponseFormat.from_body(body)
    if response_format and not body.get("tools"):
        # Keep history validation even when the structured-output executor is
        # selected (including role order and orphan tool results).
        validation = dict(body, tools=[])
        validation.pop("response_format", None)
        ExternalTools.from_body(validation)
        plain = dict(body)
        plain.pop("tools", None)
        plain.pop("tool_choice", None)
        external = ExternalTools.from_body(plain)
    else:
        external = ExternalTools.from_body(body)
    profile = TurnProfile()
    slugs = await list_models(cfg)
    model, effort = apply_effort(_resolve_model(requested, cfg, slugs), effort, slugs)
    profile.mark("models_done")
    if external:
        turn = external.run(request.app.state.sessions, model=model, effort=effort, profile=profile)
    elif response_format:
        system, history, final_user = _parse_messages(body)
        turn = response_format.run(request.app.state.sessions, system=system, history=history,
                                   final_user=final_user, model=model, effort=effort, profile=profile)
    else:
        raise invalid("A client-tool execution context is required")
    return PreparedTurn(request, body, model, profile, turn)
