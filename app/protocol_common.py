"""Shared execution for the Responses and Messages wire adapters.

All tools run on the client. Every turn retains the existing private process,
workspace, schema validation, cancellation cleanup and usage accounting.
"""
from __future__ import annotations

import asyncio
from . import json_codec as json
import time
from dataclasses import dataclass

from anyio import CancelScope
from fastapi import Request

from .agy import AgyError, list_models
from .model_policy import require_enabled_model
from .external_tools import ExternalTools
from .images import ImageInput
from .latency import LatencyTrace
from .openai_api import _parse_messages, _resolve_model
from .reasoning import ReasoningError, apply_effort, parse_effort
from .runner import TurnProfile, TurnResult, record_dashboard_usage
from .structured_output import ResponseFormat
from .session_context import client_session_key


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
    return b"event: " + event["type"].encode() + b"\ndata: " + json.dumps(event) + b"\n\n"


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
        owner = asyncio.current_task()
        finished = asyncio.Event()

        async def disconnect():
            while not finished.is_set():
                if (await self.request.receive())["type"] == "http.disconnect":
                    owner.cancel()
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(disconnect()) if not self.body.get("stream") else None
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
            self.profile.error_code = exc.code
            raise
        finally:
            with CancelScope(shield=True):
                if watcher:
                    finished.set()
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                await self.turn.aclose()
                await record_dashboard_usage(
                    self.request.app.state.cfg, self.request.app.state,
                    model=self.model, request=self.request,
                    usage=self.profile.usage,
                    duration_ms=(time.monotonic() - started) * 1000, status=status,
                )

    async def waiting(self):
        """Keep the HTTP stream alive while the validated tool result is built."""
        task = asyncio.create_task(self.collect())
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=self.request.app.state.cfg.stream_heartbeat)
                if not done:
                    yield None
            yield task.result()
        finally:
            with CancelScope(shield=True):
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def prepare(request: Request, body: dict, *, session_body: dict | None = None) -> PreparedTurn:
    trace = getattr(request.state, "latency_trace", None) or LatencyTrace()
    with trace.span("prepare"):
        return await _prepare(request, body, session_body=session_body, trace=trace)


async def _prepare(request: Request, body: dict, *, session_body: dict | None, trace) -> PreparedTurn:
    cfg = request.app.state.cfg
    session_key = client_session_key(request, session_body if session_body is not None else body)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        raise invalid("messages must be a nonempty array of objects")
    requested = string(body.get("model"), "model", nonempty=True)
    images = await ImageInput.prepare(body)
    execution = dict(body, messages=images.messages)
    boolean(body, "stream")
    if body.get("n", 1) != 1:
        raise invalid("Only n=1 is supported")
    effort = parse_effort(body.get("reasoning_effort"))
    if effort is None and cfg.effort:
        try:
            effort = parse_effort(cfg.effort)
        except ReasoningError:
            raise AgyError("AGY_PROXY_EFFORT is invalid", status_code=500, code="configuration_error") from None
    # Validate before looking up models or leasing any process.
    response_format = ResponseFormat.from_body(body)
    if response_format and not body.get("tools"):
        # Keep history validation even when the structured-output executor is
        # selected (including role order and orphan tool results).
        validation = dict(execution, tools=[])
        validation.pop("response_format", None)
        ExternalTools.from_body(validation)
        external = None
    else:
        external = ExternalTools.from_body(dict(execution, tools=execution.get("tools", [])))
    profile = TurnProfile(request_id=getattr(request.state, "agy_request_id", ""),
                          protocol=getattr(request.state, "agy_protocol", "internal"), trace=trace)
    request.state.turn_profile = profile
    try:
        with trace.span("models_lookup"):
            slugs = await list_models(cfg, owner=session_key.owner, model=requested)
    except AgyError:
        if effort is not None:
            raise
        slugs = []
    model = _resolve_model(requested, cfg, slugs)
    require_enabled_model(request, requested, model)
    model, effort = apply_effort(model, effort, slugs)
    profile.mark("models_done")
    if external:
        turn = external.run(request.app.state.sessions, model=model, effort=effort, profile=profile,
                            images=images, session_key=session_key)
    elif response_format:
        system, history, final_user = _parse_messages(execution)
        turn = response_format.run(request.app.state.sessions, system=system, history=history,
                                   final_user=final_user, model=model, effort=effort, profile=profile,
                                   images=images, session_key=session_key)
    else:
        raise invalid("A client-tool execution context is required")
    return PreparedTurn(request, body, model, profile, turn)
