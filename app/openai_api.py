"""OpenAI-compatible chat completion routes backed by agy."""

from __future__ import annotations

from . import json_codec as json
import logging
import time
import uuid
from dataclasses import replace
from contextlib import aclosing
from typing import AsyncIterator

from anyio import CancelScope
from fastapi import APIRouter, Request
from fastapi.responses import Response
from .json_codec import JSONResponse

from .agy import AgyError, list_models, resolve_model
from .model_policy import enabled_models, require_enabled_model
from .auth import is_authorized, openai_error
from .config import Config
from .protocol_adapters import ADAPTERS
from .reasoning import ReasoningError, model_families
from .images import MAX_REQUEST_BYTES
from .runner import chain_events, record_dashboard_usage
from .session_context import client_session_key
from .streaming import TurnStreamingResponse, heartbeat_events
from .token_usage import inclusive_counts

log = logging.getLogger("agy_proxy.openai")

router = APIRouter()

_ERROR_TYPES = {
    400: "invalid_request_error",
    404: "invalid_request_error",
    409: "invalid_request_error",
    413: "invalid_request_error",
    401: "authentication_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "api_error",
    503: "service_unavailable",
    504: "timeout",
}


def _err_type(code: int) -> str:
    return _ERROR_TYPES.get(code, "api_error")


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _parse_messages(body: dict) -> tuple[str, list[tuple[str, str]], str | None]:
    """Return (system, history, final_user_message)."""
    system_parts: list[str] = []
    messages: list[tuple[str, str]] = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        text = _content_text(msg.get("content"))
        if role in ("system", "developer"):
            if text.strip():
                system_parts.append(text)
            continue
        if role == "tool":
            note = f"[tool output {msg.get('tool_call_id', '')}] {text}".strip()
            if note:
                messages.append(("tool", note))
            continue
        if role == "assistant":
            text = text.strip()
            calls = msg.get("tool_calls")
            if isinstance(calls, list) and calls:
                names = ", ".join(
                    str(c.get("function", {}).get("name", "?")) for c in calls if isinstance(c, dict)
                )
                text = (text + f"\n[assistant requested tools: {names}]").strip()
            if text:
                messages.append(("assistant", text))
            continue
        if text.strip():
            messages.append(("user", text))

    final_user: str | None = None
    history = messages
    if messages and messages[-1][0] == "user":
        final_user = messages[-1][1]
        history = messages[:-1]
    return "\n\n".join(system_parts), history, final_user


def _usage(usage: dict) -> dict:
    inp, out, total = inclusive_counts(usage)
    return {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": total,
            "prompt_tokens_details": {"cached_tokens": usage.get("cache_read_tokens", 0)},
            "completion_tokens_details": {"reasoning_tokens": usage.get("thinking_tokens", 0)}}


async def _read_json(request: Request) -> dict | None:
    chunks, size = [], 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_REQUEST_BYTES:
                raise AgyError("Request body exceeds the 32 MiB limit", status_code=413)
            chunks.append(chunk)
        body = json.loads(b"".join(chunks))
    except AgyError:
        raise
    except (ValueError, UnicodeError):
        return None
    return body if isinstance(body, dict) else None


def _resolve_model(requested: str, cfg: Config, slugs: list[str]) -> str | None:
    # Explicit defaults and configured custom aliases remain supported. An
    # unknown explicit name must never silently fall back to the default model.
    if requested in cfg.model_map:
        return cfg.model_map[requested]
    if not requested or requested.lower() in ("default", "(default)"):
        return cfg.default_model
    if requested.lower() == "auto":
        return "auto"
    resolved = resolve_model(requested, replace(cfg, default_model=None), slugs)
    if slugs and resolved not in slugs:
        raise AgyError(f"Model '{requested}' not found", status_code=404, code="model_not_found")
    return resolved


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    cfg = request.app.state.cfg
    if not is_authorized(cfg, request):
        return ADAPTERS["chat"].error(401, "Invalid API key", "authentication_error", "invalid_api_key")

    try:
        body = await _read_json(request)
    except AgyError as exc:
        return ADAPTERS["chat"].error(exc.status_code, str(exc), _err_type(exc.status_code), exc.code)
    if body is None:
        return ADAPTERS["chat"].error(400, "Request body is not valid JSON", "invalid_request_error")

    from .protocol_common import prepare
    try:
        options = body.get("stream_options")
        if options is not None and (not isinstance(options, dict) or (
            "include_usage" in options and not isinstance(options["include_usage"], bool)
        )):
            raise AgyError("stream_options.include_usage must be a boolean", status_code=400)
        prepared = await prepare(request, ADAPTERS["chat"].parse(body), session_body=body)
    except AgyError as exc:
        return ADAPTERS["chat"].error(exc.status_code, str(exc), _err_type(exc.status_code), exc.code,
                            param="reasoning_effort" if isinstance(exc, ReasoningError) else
                            "model" if exc.code == "model_not_found" else None)
    turn, profile, model = prepared.turn, prepared.profile, prepared.model
    state = request.app.state
    requested_model = body.get("model") or ""

    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    stream = bool(body.get("stream"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    def chunk(delta: dict, finish: str | None) -> dict:
        return {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": requested_model or model or "agy-default",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    if not stream:
        try:
            result = await prepared.collect()
        except AgyError as exc:
            return ADAPTERS["chat"].error(exc.status_code, str(exc), _err_type(exc.status_code), exc.code)
        text = result.text if result else ""
        usage = _usage(result.usage) if result else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": requested_model or model or "agy-default",
                "choices": [
                    {"index": 0, "message": ADAPTERS["chat"].encode(result) if result else {"role": "assistant", "content": text},
                     "finish_reason": result.finish_reason if result else "stop"}
                ],
                "usage": usage,
            },
            headers={"X-Agy-Profile": profile.summary()},
        )

    # Preflight only process acquisition. Do not wait for model output before
    # opening SSE: thinking and validated tool JSON may be silent for minutes.
    try:
        first = await turn.__anext__()
    except StopAsyncIteration:
        first = None
    except AgyError as exc:
        await record_dashboard_usage(cfg, state, model=model, request=request, usage=None,
                                     duration_ms=profile.total_ms, status=f"ERROR {exc.status_code}")
        return ADAPTERS["chat"].error(exc.status_code, str(exc), _err_type(exc.status_code), exc.code)

    async def sse() -> AsyncIterator[bytes]:
        def encode(obj: dict) -> bytes:
            return b"data: " + json.dumps(obj) + b"\n\n"

        recorded = False

        async def account(usage, status: str) -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            await record_dashboard_usage(
                cfg, request.app.state, model=model, request=request, usage=usage,
                duration_ms=profile.total_ms, status=status,
            )

        try:
            request.state.start_event_sent = True
            yield encode(chunk({"role": "assistant", "content": ""}, None))
            async with aclosing(heartbeat_events(chain_events([] if first is None else [first], turn),
                                                 cfg.stream_heartbeat)) as pending:
                async for event in pending:
                    if event is None:
                        yield b": keep-alive\n\n"
                        continue
                    kind, payload = event
                    if kind == "delta":
                        request.state.business_delivered = True
                        yield encode(chunk({"content": payload}, None))
                    elif kind == "result":
                        request.state.business_delivered = True
                        await account(payload.usage, "SUCCESS")
                        for value in ADAPTERS["chat"].stream(payload, chunk=chunk, include_usage=include_usage):
                            yield encode(value)
                        yield b"data: [DONE]\n\n"
                        yield b": agy-profile " + profile.summary().encode("utf-8") + b"\n\n"
                        return
        except AgyError as exc:
            await account(None, f"ERROR {exc.status_code}")
            yield encode({"error": {"message": str(exc), "type": _err_type(exc.status_code), "code": exc.code}})
        except BaseException:
            # Client disconnect / cancellation: keep the session's accounting
            # honest so the dashboard shows aborted turns too.
            with CancelScope(shield=True):
                await turn.aclose()
                await account(None, "ABORTED")
            raise
        finally:
            # Closing the HTTP iterator must release the upstream lease even
            # when the client disconnects after the initial role-only chunk.
            with CancelScope(shield=True):
                await turn.aclose()

    return TurnStreamingResponse(
        sse(), turn=turn,
        on_close=lambda: record_dashboard_usage(cfg, state, model=model, request=request, usage=None,
                                                duration_ms=profile.total_ms, status="ABORTED"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/v1/models")
async def models_list(request: Request) -> Response:
    cfg = request.app.state.cfg
    if not is_authorized(cfg, request):
        return openai_error(401, "Invalid API key", "authentication_error", "invalid_api_key")
    try:
        slugs = await list_models(cfg, owner=client_session_key(request).owner)
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code), exc.code)
    return JSONResponse(
        {
            "object": "list",
            "data": [{"id": s, "object": "model", "created": 0, "owned_by": "antigravity"}
                     for s in enabled_models(request, list(dict.fromkeys([*slugs, *model_families(slugs)])))],
        }
    )


@router.get("/v1/models/{model_id}")
async def model_detail(model_id: str, request: Request) -> Response:
    cfg = request.app.state.cfg
    if not is_authorized(cfg, request):
        return openai_error(401, "Invalid API key", "authentication_error", "invalid_api_key")
    try:
        slugs = await list_models(cfg, owner=client_session_key(request).owner, model=model_id)
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code), exc.code)
    try:
        resolved = _resolve_model(model_id, cfg, slugs)
        require_enabled_model(request, model_id, resolved)
    except AgyError:
        return openai_error(404, f"Model '{model_id}' not found", "invalid_request_error", "model_not_found")
    if model_id in slugs or resolved in slugs:
        return JSONResponse({"id": model_id, "object": "model", "created": 0, "owned_by": "antigravity"})
    return openai_error(404, f"Model '{model_id}' not found", "invalid_request_error", "model_not_found")
