"""Gemini API (generativelanguage) compatible routes backed by agy."""

from __future__ import annotations

from . import json_codec as json
import logging
import uuid
from contextlib import aclosing
from typing import AsyncIterator

from anyio import CancelScope
from fastapi import APIRouter, Request
from fastapi.responses import Response
from .json_codec import JSONResponse

from .agy import AgyError, list_models
from .model_policy import enabled_models, require_enabled_model
from .auth import is_authorized
from .protocol_adapters import ADAPTERS
from .runner import chain_events, record_dashboard_usage
from .openai_api import _read_json
from .token_usage import inclusive_counts
from .session_context import client_session_key
from .streaming import TurnStreamingResponse, heartbeat_events

log = logging.getLogger("agy_proxy.gemini")

router = APIRouter()

_ACTIONS = (
    (":streamGenerateContent", "stream"),
    (":generateContent", "generate"),
    (":countTokens", "count"),
    ("/streamGenerateContent", "stream"),
    ("/generateContent", "generate"),
    ("/countTokens", "count"),
)

_STATUS_NAMES = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    413: "INVALID_ARGUMENT",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    502: "INTERNAL",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}


def _status_name(code: int) -> str:
    return _STATUS_NAMES.get(code, "INTERNAL")


def _split_action(rest: str) -> tuple[str, str] | None:
    target = rest[len("models/"):] if rest.startswith("models/") else rest
    for suffix, action in _ACTIONS:
        if target.endswith(suffix):
            return target[: -len(suffix)], action
    return None


def _parts_text(parts) -> str:
    if isinstance(parts, str):
        return parts
    texts: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return "".join(texts)


def _usage_meta(usage: dict) -> dict:
    inp, out, total = inclusive_counts(usage)
    think = usage.get("thinking_tokens") or 0
    meta = {
        "promptTokenCount": inp,
        "candidatesTokenCount": max(0, out - think),
        "totalTokenCount": total,
    }
    if think:
        meta["thoughtsTokenCount"] = think
    if usage.get("cache_read_tokens"):
        meta["cachedContentTokenCount"] = usage["cache_read_tokens"]
    return meta


@router.post("/v1beta/{rest:path}")
@router.post("/v1/{rest:path}")
async def gemini_action(rest: str, request: Request) -> Response:
    cfg = request.app.state.cfg
    if not is_authorized(cfg, request):
        return ADAPTERS["gemini"].error(401, "API key not valid", "UNAUTHENTICATED")

    parsed = _split_action(rest)
    if parsed is None:
        return ADAPTERS["gemini"].error(404, f"Unknown Gemini endpoint: /{rest}", "NOT_FOUND")
    model_requested, action = parsed
    try:
        require_enabled_model(request, model_requested)
    except AgyError as exc:
        return ADAPTERS["gemini"].error(exc.status_code, f"{exc} [{exc.code}; {request.state.agy_request_id}]", _status_name(exc.status_code))

    try:
        body = await _read_json(request)
    except AgyError as exc:
        return ADAPTERS["gemini"].error(exc.status_code, f"{exc} [{exc.code}; {request.state.agy_request_id}]", _status_name(exc.status_code))
    if body is None:
        return ADAPTERS["gemini"].error(400, "Request body is not valid JSON", "INVALID_ARGUMENT")

    if action == "count":
        contents = body.get("contents") or []
        approx = sum(len(_parts_text(c.get("parts"))) for c in contents if isinstance(c, dict)) // 4
        return JSONResponse({"totalTokens": max(1, approx)})

    from .protocol_common import prepare
    try:
        chat = ADAPTERS["gemini"].parse(body, model=model_requested, stream=action == "stream")
        prepared = await prepare(request, chat, session_body=body)
    except AgyError as exc:
        return ADAPTERS["gemini"].error(exc.status_code, f"{exc} [{exc.code}; {request.state.agy_request_id}]", _status_name(exc.status_code))
    turn, profile, model = prepared.turn, prepared.profile, prepared.model
    state = request.app.state
    response_id = "agy-" + uuid.uuid4().hex[:24]
    if action == "generate":
        try:
            result = await prepared.collect()
        except AgyError as exc:
            return ADAPTERS["gemini"].error(exc.status_code, f"{exc} [{exc.code}; {request.state.agy_request_id}]", _status_name(exc.status_code))
        body_out = ADAPTERS["gemini"].encode(result, model=model, response_id=response_id)
        return JSONResponse(body_out, headers={"X-Agy-Profile": profile.summary()})

    # action == "stream": Gemini SSE (alt=sse is what real clients use)
    # Pull the first event before opening the SSE body so startup failures
    # (missing binary, dead session) return a real HTTP error status.
    try:
        first = await turn.__anext__()
    except StopAsyncIteration:
        first = None
    except AgyError as exc:
        await record_dashboard_usage(cfg, state, model=model, request=request, usage=None,
                                     duration_ms=profile.total_ms, status=f"ERROR {exc.status_code}")
        return ADAPTERS["gemini"].error(exc.status_code, f"{exc} [{exc.code}; {request.state.agy_request_id}]", _status_name(exc.status_code))
    response_id = "agy-" + uuid.uuid4().hex[:24]

    async def sse() -> AsyncIterator[bytes]:
        recorded = False

        async def account(usage, status: str) -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            await record_dashboard_usage(
                cfg, state, model=model, request=request, usage=usage,
                duration_ms=profile.total_ms, status=status,
            )

        try:
            request.state.start_event_sent = True
            yield b": connected\n\n"
            async with aclosing(heartbeat_events(chain_events([] if first is None else [first], turn),
                                                 cfg.stream_heartbeat)) as pending:
                async for event in pending:
                    if event is None:
                        yield b": keep-alive\n\n"
                        continue
                    kind, payload = event
                    if kind == "result":
                        request.state.business_delivered = True
                        await account(payload.usage, "SUCCESS")
                        for chunk in ADAPTERS["gemini"].stream(payload, model=model, response_id=response_id):
                            yield b"data: " + json.dumps(chunk) + b"\n\n"
                        yield b": agy-profile " + profile.summary().encode("utf-8") + b"\n\n"
                        return
        except AgyError as exc:
            await account(None, f"ERROR {exc.status_code}")
            err = {"error": {"code": exc.status_code, "message": f"{exc} [{exc.code}; {request.state.agy_request_id}]", "status": _status_name(exc.status_code)}}
            yield b"data: " + json.dumps(err) + b"\n\n"
        except BaseException:
            with CancelScope(shield=True):
                await turn.aclose()
                await account(None, "ABORTED")
            raise
        finally:
            with CancelScope(shield=True):
                await turn.aclose()

    return TurnStreamingResponse(
        sse(), turn=turn,
        on_close=lambda: record_dashboard_usage(cfg, state, model=model, request=request, usage=None,
                                                duration_ms=profile.total_ms, status="ABORTED"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/v1beta/models")
async def gemini_models(request: Request) -> Response:
    cfg = request.app.state.cfg
    if not is_authorized(cfg, request):
        return ADAPTERS["gemini"].error(401, "API key not valid", "UNAUTHENTICATED")
    try:
        slugs = await list_models(cfg, owner=client_session_key(request).owner)
    except AgyError as exc:
        return ADAPTERS["gemini"].error(exc.status_code, f"{exc} [{exc.code}; {request.state.agy_request_id}]", _status_name(exc.status_code))
    models = [
        {
            "name": f"models/{slug}",
            "displayName": slug,
            "version": "1",
            "supportedGenerationMethods": ["generateContent", "streamGenerateContent", "countTokens"],
        }
        for slug in enabled_models(request, slugs)
    ]
    return JSONResponse({"models": models})
