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
from .auth import api_key_record, is_authorized, openai_error
from .config import Config
from .reasoning import ReasoningError, apply_effort, model_families, parse_effort
from .external_tools import ExternalTools
from .images import ImageInput, MAX_REQUEST_BYTES
from .runner import TurnProfile, TurnResult, chain_events, record_dashboard_usage, run_turn
from .structured_output import ResponseFormat
from .session_context import client_session_key
from .streaming import TurnStreamingResponse, heartbeat_events

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
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    total = usage.get("total_tokens") or (inp + out)
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
        raise AgyError(f"Model '{requested}' not found", status_code=404)
    return resolved


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    cfg = request.app.state.cfg
    if not is_authorized(cfg, request):
        return openai_error(401, "Invalid API key", "authentication_error", "invalid_api_key")

    try:
        body = await _read_json(request)
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))
    if body is None:
        return openai_error(400, "Request body is not valid JSON", "invalid_request_error")

    try:
        effort = parse_effort(body.get("reasoning_effort"))
    except ReasoningError as exc:
        return openai_error(400, str(exc), "invalid_request_error", "invalid_value", param="reasoning_effort")
    if effort is None and cfg.effort:
        try:
            effort = parse_effort(cfg.effort)
        except ReasoningError:
            return openai_error(500, "AGY_PROXY_EFFORT is invalid", "server_error")

    try:
        session_key = client_session_key(request, body)
        if body.get("model") is not None and not isinstance(body["model"], str):
            raise AgyError("model must be a string", status_code=400)
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
            raise AgyError("messages must be a nonempty array of objects", status_code=400)
        if "stream" in body and not isinstance(body["stream"], bool):
            raise AgyError("stream must be a boolean", status_code=400)
        options = body.get("stream_options")
        if options is not None and (not isinstance(options, dict) or (
            "include_usage" in options and not isinstance(options["include_usage"], bool)
        )):
            raise AgyError("stream_options.include_usage must be a boolean", status_code=400)
        response_format = ResponseFormat.from_body(body)
        images = await ImageInput.prepare(body, cfg.max_prompt_chars)
        execution = dict(body, messages=images.messages)
        if images.attachments and not response_format:
            execution.setdefault("tools", [])
        external = ExternalTools.from_body(execution)
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))
    system, history, final_user = _parse_messages(execution)
    if not history and final_user is None:
        return openai_error(400, "Request has no messages with text content", "invalid_request_error")
    if len(system) + sum(len(t) for _, t in history) + len(final_user or "") > cfg.max_prompt_chars:
        return openai_error(413, "Prompt too long for this proxy", "invalid_request_error")

    requested_model = body.get("model") or ""
    profile = TurnProfile()
    try:
        slugs = await list_models(cfg)
    except AgyError as exc:
        if effort is not None:
            # A base name cannot be mapped safely when the model catalog is
            # unavailable. Do not silently select a different effort/model.
            return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))
        slugs = []
    profile.mark("models_done")
    try:
        model = _resolve_model(requested_model, cfg, slugs)
        require_enabled_model(request, requested_model, model)
    except AgyError as exc:
        return openai_error(404, str(exc), "invalid_request_error", "model_not_found", param="model")
    try:
        model, effort = apply_effort(model, effort, slugs)
    except ReasoningError as exc:
        return openai_error(400, str(exc), "invalid_request_error", "unsupported_value", param="reasoning_effort")

    state = request.app.state
    if external:
        turn = external.run(state.sessions, model=model, effort=effort, profile=profile, images=images,
                            session_key=session_key)
    elif response_format:
        turn = response_format.run(
            state.sessions,
            system=system, history=history, final_user=final_user,
            model=model, effort=effort, profile=profile, images=images, session_key=session_key,
        )
    else:
        turn = run_turn(
            cfg, state.sessions,
            system=system, history=history, final_user=final_user,
            model=model, effort=effort, profile=profile, session_key=session_key,
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
            if result is None:
                raise AgyError("agy ended without a result")
        except AgyError as exc:
            await record_dashboard_usage(
                cfg, request.app.state, model=model, request=request, usage=None,
                duration_ms=(time.monotonic() - t0) * 1000, status=f"ERROR {exc.status_code}",
            )
            return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))
        finally:
            with CancelScope(shield=True):
                await turn.aclose()
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

    # Preflight only process acquisition. Do not wait for model output before
    # opening SSE: thinking and validated tool JSON may be silent for minutes.
    try:
        first = await turn.__anext__()
    except StopAsyncIteration:
        first = None
    except AgyError as exc:
        await record_dashboard_usage(cfg, state, model=model, request=request, usage=None,
                                     duration_ms=profile.total_ms, status=f"ERROR {exc.status_code}")
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))

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
            yield encode(chunk({"role": "assistant", "content": ""}, None))
            async with aclosing(heartbeat_events(chain_events([] if first is None else [first], turn),
                                                 cfg.stream_heartbeat)) as pending:
                async for event in pending:
                    if event is None:
                        yield b": keep-alive\n\n"
                        continue
                    kind, payload = event
                    if kind == "delta":
                        yield encode(chunk({"content": payload}, None))
                    elif kind == "result":
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
        finally:
            # Closing the HTTP iterator must release the upstream lease even
            # when the client disconnects after the initial role-only chunk.
            with CancelScope(shield=True):
                await turn.aclose()

    return TurnStreamingResponse(
        sse(), turn=turn,
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
        slugs = await list_models(cfg)
    except AgyError as exc:
        return openai_error(exc.status_code, str(exc), _err_type(exc.status_code))
    try:
        resolved = _resolve_model(model_id, cfg, slugs)
        require_enabled_model(request, model_id, resolved)
    except AgyError:
        return openai_error(404, f"Model '{model_id}' not found", "invalid_request_error", "model_not_found")
    if model_id in slugs or resolved in slugs:
        return JSONResponse({"id": model_id, "object": "model", "created": 0, "owned_by": "antigravity"})
    return openai_error(404, f"Model '{model_id}' not found", "invalid_request_error", "model_not_found")
