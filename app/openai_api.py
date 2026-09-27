"""OpenAI-compatible chat completion routes backed by agy."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .agy import AgyError, list_models, resolve_model
from .auth import api_key_record, is_authorized, openai_error
from .external_tools import ExternalTools
from .runner import TurnProfile, TurnResult, chain_events, record_dashboard_usage, run_turn

log = logging.getLogger("agy_proxy.openai")

router = APIRouter()

_ERROR_TYPES = {
    400: "invalid_request_error",
    404: "invalid_request_error",
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


def _effort(body: dict) -> str | None:
    raw = body.get("reasoning_effort")
    if isinstance(raw, str):
        level = raw.lower()
        if level == "minimal":
            return "low"
        if level in ("low", "medium", "high"):
            return level
    return None


def _usage(usage: dict) -> dict:
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    total = usage.get("total_tokens") or (inp + out + (usage.get("thinking_tokens") or 0))
    return {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": total}


async def _read_json(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    cfg = request.app.state.cfg
    if not is_authorized(cfg, request):
        return openai_error(401, "Invalid API key", "authentication_error", "invalid_api_key")

    body = await _read_json(request)
    if body is None:
        return openai_error(400, "Request body is not valid JSON", "invalid_request_error")

    try:
        external = ExternalTools.from_body(body)
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))
    if external and len(json.dumps(body, ensure_ascii=False)) > cfg.max_prompt_chars:
        return openai_error(413, "Prompt and tool definitions too long for this proxy", "invalid_request_error")
    system, history, final_user = _parse_messages(body)
    if not history and final_user is None:
        return openai_error(400, "Request has no messages with text content", "invalid_request_error")
    if len(system) + sum(len(t) for _, t in history) + len(final_user or "") > cfg.max_prompt_chars:
        return openai_error(413, "Prompt too long for this proxy", "invalid_request_error")

    requested_model = body.get("model") or ""
    profile = TurnProfile()
    try:
        slugs = await list_models(cfg)
    except AgyError:
        slugs = []
    profile.mark("models_done")
    model = resolve_model(requested_model, cfg, slugs)
    effort = _effort(body)

    state = request.app.state
    turn = external.run(state.sessions, model=model, effort=effort, profile=profile) if external else run_turn(
        cfg, state.sessions,
        system=system, history=history, final_user=final_user,
        model=model, effort=effort, profile=profile,
    )

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
        result: TurnResult | None = None
        t0 = time.monotonic()
        try:
            async for kind, payload in turn:
                if kind == "result":
                    result = payload
        except AgyError as exc:
            await record_dashboard_usage(
                cfg, request.app.state, model=model, request=request, usage=None,
                duration_ms=(time.monotonic() - t0) * 1000, status=f"ERROR {exc.status_code}",
            )
            return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))
        await record_dashboard_usage(
            cfg, request.app.state, model=model, request=request,
            usage=result.usage if result else None,
            duration_ms=(time.monotonic() - t0) * 1000,
        )
        text = result.text if result else ""
        usage = _usage(result.usage) if result else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": requested_model or model or "agy-default",
                "choices": [
                    {"index": 0, "message": result.message() if result else {"role": "assistant", "content": text},
                     "finish_reason": result.finish_reason if result else "stop"}
                ],
                "usage": usage,
            },
            headers={"X-Agy-Profile": profile.summary()},
        )

    # Streaming: pull the first event before starting the SSE body so that
    # startup failures (missing binary, dead session) return a real HTTP error.
    try:
        first = await turn.__anext__()
    except StopAsyncIteration:
        first = None
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))

    async def sse() -> AsyncIterator[bytes]:
        def encode(obj: dict) -> bytes:
            return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"

        t0 = time.monotonic()
        recorded = False

        async def account(usage, status: str) -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            await record_dashboard_usage(
                cfg, request.app.state, model=model, request=request, usage=usage,
                duration_ms=(time.monotonic() - t0) * 1000, status=status,
            )

        yield encode(chunk({"role": "assistant", "content": ""}, None))
        try:
            async for kind, payload in chain_events([] if first is None else [first], turn):
                if kind == "delta":
                    yield encode(chunk({"content": payload}, None))
                else:
                    await account(payload.usage, "SUCCESS")
                    if payload.tool_calls:
                        yield encode(chunk({"tool_calls": [dict(call, index=i)
                                                           for i, call in enumerate(payload.tool_calls)]}, None))
                    yield encode(chunk({}, payload.finish_reason))
                    if include_usage:
                        yield encode(
                            {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": requested_model or model or "agy-default",
                                "choices": [],
                                "usage": _usage(payload.usage),
                            }
                        )
                    yield b"data: [DONE]\n\n"
                    yield b": agy-profile " + profile.summary().encode("utf-8") + b"\n\n"
                    return
        except AgyError as exc:
            await account(None, f"ERROR {exc.status_code}")
            yield encode({"error": {"message": str(exc), "type": _err_type(exc.status_code)}})
            yield b"data: [DONE]\n\n"
        except BaseException:
            # Client disconnect / cancellation: keep the session's accounting
            # honest so the dashboard shows aborted turns too.
            await account(None, "ABORTED")
            raise

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/v1/models")
async def models_list(request: Request) -> Response:
    cfg = request.app.state.cfg
    if not is_authorized(cfg, request):
        return openai_error(401, "Invalid API key", "authentication_error", "invalid_api_key")
    try:
        slugs = await list_models(cfg)
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))
    return JSONResponse(
        {
            "object": "list",
            "data": [{"id": s, "object": "model", "created": 0, "owned_by": "antigravity"} for s in slugs],
        }
    )


@router.get("/v1/models/{model_id}")
async def model_detail(model_id: str, request: Request) -> Response:
    cfg = request.app.state.cfg
    if not is_authorized(cfg, request):
        return openai_error(401, "Invalid API key", "authentication_error", "invalid_api_key")
    try:
        slugs = await list_models(cfg)
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))
    resolved = resolve_model(model_id, cfg, slugs)
    if model_id in slugs or resolved in slugs:
        return JSONResponse({"id": model_id, "object": "model", "created": 0, "owned_by": "antigravity"})
    return openai_error(404, f"Model '{model_id}' not found", "invalid_request_error", "model_not_found")
