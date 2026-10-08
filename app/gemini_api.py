"""Gemini API (generativelanguage) compatible routes backed by agy."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import AsyncIterator

from anyio import CancelScope
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .agy import AgyError, list_models, resolve_model
from .auth import gemini_error, is_authorized
from .runner import TurnProfile, TurnResult, chain_events, record_dashboard_usage, run_turn

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
            elif isinstance(part, dict) and ("inlineData" in part or "inline_data" in part or "fileData" in part):
                texts.append("[non-text content omitted]")
            elif isinstance(part, dict) and ("functionCall" in part or "function_call" in part):
                texts.append("[function call omitted]")
    return "".join(texts)


def _parse_generate_body(body: dict) -> tuple[str, list[tuple[str, str]], str | None, str | None]:
    """Return (system, history, final_user, effort)."""
    sys_text = ""
    sys_inst = body.get("systemInstruction") or body.get("system_instruction")
    if isinstance(sys_inst, str):
        sys_text = sys_inst
    elif isinstance(sys_inst, dict):
        sys_text = _parts_text(sys_inst.get("parts"))

    messages: list[tuple[str, str]] = []
    for content in body.get("contents") or []:
        if not isinstance(content, dict):
            continue
        role = content.get("role", "user")
        norm_role = "assistant" if role == "model" else ("tool" if role in ("function", "tool") else "user")
        text = _parts_text(content.get("parts"))
        if text.strip():
            messages.append((norm_role, text))

    final_user: str | None = None
    history = messages
    if messages and messages[-1][0] == "user":
        final_user = messages[-1][1]
        history = messages[:-1]

    gen_cfg = body.get("generationConfig") or body.get("generation_config") or {}
    effort = None
    if isinstance(gen_cfg, dict):
        thinking = gen_cfg.get("thinkingConfig") or gen_cfg.get("thinking_config") or {}
        if isinstance(thinking, dict):
            level = thinking.get("thinkingLevel") or thinking.get("thinking_level")
            if isinstance(level, str) and level.lower() in ("low", "medium", "high"):
                effort = level.lower()
    return sys_text, history, final_user, effort


def _usage_meta(usage: dict) -> dict:
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    think = usage.get("thinking_tokens") or 0
    total = usage.get("total_tokens") or (inp + out + think)
    meta = {
        "promptTokenCount": inp,
        "candidatesTokenCount": out,
        "totalTokenCount": total,
    }
    if think:
        meta["thoughtsTokenCount"] = think
    if usage.get("cache_read_tokens"):
        meta["cachedContentTokenCount"] = usage["cache_read_tokens"]
    return meta


def _candidates(text: str, finish: str | None, usage: dict | None) -> dict:
    candidate: dict = {"content": {"role": "model", "parts": [{"text": text}]}, "index": 0}
    if finish:
        candidate["finishReason"] = finish
    body: dict = {"candidates": [candidate]}
    if usage:
        body["usageMetadata"] = _usage_meta(usage)
    return body


async def _read_json(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@router.post("/v1beta/{rest:path}")
@router.post("/v1/{rest:path}")
async def gemini_action(rest: str, request: Request) -> Response:
    cfg = request.app.state.cfg
    if not is_authorized(cfg, request):
        return gemini_error(401, "API key not valid", "UNAUTHENTICATED")

    parsed = _split_action(rest)
    if parsed is None:
        return gemini_error(404, f"Unknown Gemini endpoint: /{rest}", "NOT_FOUND")
    model_requested, action = parsed

    body = await _read_json(request)
    if body is None:
        return gemini_error(400, "Request body is not valid JSON", "INVALID_ARGUMENT")

    if action == "count":
        contents = body.get("contents") or []
        approx = sum(len(_parts_text(c.get("parts"))) for c in contents if isinstance(c, dict)) // 4
        return JSONResponse({"totalTokens": max(1, approx)})

    system, history, final_user, effort = _parse_generate_body(body)
    if not history and final_user is None:
        return gemini_error(400, "Request has no text content", "INVALID_ARGUMENT")
    if len(system) + sum(len(t) for _, t in history) + len(final_user or "") > cfg.max_prompt_chars:
        return gemini_error(413, "Prompt too long for this proxy", "INVALID_ARGUMENT")

    profile = TurnProfile()
    try:
        slugs = await list_models(cfg)
    except AgyError:
        slugs = []
    profile.mark("models_done")
    model = resolve_model(model_requested, cfg, slugs)

    state = request.app.state
    if action == "generate":
        result: TurnResult | None = None
        t0 = time.monotonic()
        try:
            async for kind, payload in run_turn(
                cfg, state.sessions,
                system=system, history=history, final_user=final_user,
                model=model, effort=effort, profile=profile,
            ):
                if kind == "result":
                    result = payload
        except AgyError as exc:
            await record_dashboard_usage(
                cfg, state, model=model, request=request, usage=None,
                duration_ms=(time.monotonic() - t0) * 1000, status=f"ERROR {exc.status_code}",
            )
            return gemini_error(exc.status_code, str(exc), _status_name(exc.status_code))
        await record_dashboard_usage(
            cfg, state, model=model, request=request,
            usage=result.usage if result else None,
            duration_ms=(time.monotonic() - t0) * 1000,
        )
        body_out = _candidates(result.text if result else "", "STOP", result.usage if result else None)
        body_out["modelVersion"] = model or "agy-default"
        return JSONResponse(body_out, headers={"X-Agy-Profile": profile.summary()})

    # action == "stream": Gemini SSE (alt=sse is what real clients use)
    turn = run_turn(
        cfg, state.sessions,
        system=system, history=history, final_user=final_user,
        model=model, effort=effort, profile=profile,
    )
    # Pull the first event before opening the SSE body so startup failures
    # (missing binary, dead session) return a real HTTP error status.
    try:
        first = await turn.__anext__()
    except StopAsyncIteration:
        first = None
    except AgyError as exc:
        return gemini_error(exc.status_code, str(exc), _status_name(exc.status_code))
    response_id = "agy-" + uuid.uuid4().hex[:24]

    async def sse() -> AsyncIterator[bytes]:
        t0 = time.monotonic()
        recorded = False

        async def account(usage, status: str) -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            await record_dashboard_usage(
                cfg, state, model=model, request=request, usage=usage,
                duration_ms=(time.monotonic() - t0) * 1000, status=status,
            )

        try:
            async for kind, payload in chain_events([] if first is None else [first], turn):
                if kind == "delta":
                    chunk = _candidates(payload, None, None)
                    chunk["responseId"] = response_id
                    yield b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n"
                else:
                    await account(payload.usage, "SUCCESS")
                    chunk = _candidates("", "STOP", payload.usage)
                    chunk["responseId"] = response_id
                    chunk["modelVersion"] = model or "agy-default"
                    yield b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n"
                    yield b": agy-profile " + profile.summary().encode("utf-8") + b"\n\n"
                    return
        except AgyError as exc:
            await account(None, f"ERROR {exc.status_code}")
            err = {"error": {"code": exc.status_code, "message": str(exc), "status": _status_name(exc.status_code)}}
            yield b"data: " + json.dumps(err, ensure_ascii=False).encode("utf-8") + b"\n\n"
        except BaseException:
            await account(None, "ABORTED")
            raise
        finally:
            with CancelScope(shield=True):
                await turn.aclose()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/v1beta/models")
async def gemini_models(request: Request) -> Response:
    cfg = request.app.state.cfg
    if not is_authorized(cfg, request):
        return gemini_error(401, "API key not valid", "UNAUTHENTICATED")
    try:
        slugs = await list_models(cfg)
    except AgyError as exc:
        return gemini_error(exc.status_code, str(exc), _status_name(exc.status_code))
    models = [
        {
            "name": f"models/{slug}",
            "displayName": slug,
            "version": "1",
            "supportedGenerationMethods": ["generateContent", "streamGenerateContent", "countTokens"],
        }
        for slug in slugs
    ]
    return JSONResponse({"models": models})
