"""Shared turn executor used by both the Gemini and OpenAI routers.

Seeds a fresh conversation or submits just the new messages of an identified
client session. HTTP streaming is independent of the CLI's duplex NDJSON.
"""

from __future__ import annotations

from . import json_codec as json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

from .agy import AgyError, SessionManager, account_failover
from .auth import api_key_record
from .config import Config
from .contracts import ExecutionOutcome
from .latency import LatencyTrace
from .session_context import SessionContext, SessionIdentity

log = logging.getLogger("agy_proxy.runner")


@dataclass
class TurnProfile:
    """Per-request phase timing, logged once the turn finishes."""

    started: float = field(default_factory=time.monotonic)
    model: str | None = None
    new_session: bool = False
    marks: dict[str, float] = field(default_factory=dict)
    request_id: str = ""
    protocol: str = "internal"
    attempts: int = 0
    repair_attempts: int = 0
    error_code: str | None = None
    usage_complete: bool = True
    usage: dict = field(default_factory=dict)
    trace: LatencyTrace = field(default_factory=LatencyTrace)

    def add_usage(self, usage):
        if not isinstance(usage, dict) or not usage:
            self.usage_complete = False
            return
        for name, value in usage.items():
            if type(value) is int and value >= 0:
                self.usage[name] = self.usage.get(name, 0) + value

    def diagnostics(self):
        return {"request_id": self.request_id, "protocol": self.protocol,
                "attempts": self.attempts, "repair_attempts": self.repair_attempts,
                "error_code": self.error_code, "usage_complete": self.usage_complete}

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, time.monotonic() - self.started)

    @property
    def total_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000

    def summary(self) -> str:
        if self.trace.spans:
            phases = ("models_lookup", "session_acquire", "cli_turn", "validate", "recovery_restart")
            totals = {name: sum(s.duration_ms or 0 for s in self.trace.spans if s.name == name) for name in phases}
            return " ".join([f"model={self.model}", f"new_session={int(self.new_session)}",
                             *(f"{name}={value:.1f}ms" for name, value in totals.items()),
                             f"total={self.total_ms:.0f}ms"])

        def ms(a: str, b: str | None = None) -> float:
            start = self.marks.get(a)
            if start is None:
                return -1.0
            if b is None:
                end = time.monotonic() - self.started
            else:
                end = self.marks.get(b)
                if end is None:
                    return -1.0
            return max(0.0, end - start) * 1000

        parts = [
            f"model={self.model}",
            f"new_session={int(self.new_session)}",
            f"models_lookup={ms('models_done', 'turn_start'):.0f}ms",
            f"acquire={ms('turn_start', 'session_ready'):.0f}ms",
            f"agy_boot={ms('session_ready', 'init_seen'):.0f}ms",
            f"upstream_wait={ms('init_seen' if self.marks.get('init_seen') is not None else 'session_ready', 'first_delta'):.0f}ms",
            f"stream={ms('first_delta', 'result'):.0f}ms",
            f"total={self.total_ms:.0f}ms",
        ]
        return " ".join(parts)

# history entries are (role, text) tuples; role is "user" | "assistant" | "tool"
History = list[tuple[str, str]]

TurnEvent = tuple[Literal["ready", "delta", "result"], object]


@dataclass
class TurnResult(ExecutionOutcome):
    status: str = "SUCCESS"

    @property
    def tool_calls(self):
        return [{"id": call.id, "type": "function", "function": {
            "name": call.name, "arguments": json.dumps_text(call.arguments, sort_keys=True)}} for call in self.calls]

    @property
    def finish_reason(self) -> str:
        return "tool_calls" if self.tool_calls else "stop"

    def message(self) -> dict:
        message = {"role": "assistant", "content": self.text or (None if self.tool_calls else "")}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return message


async def chain_events(first_events: list, agen) -> AsyncIterator[TurnEvent]:
    """Replay already-pulled events, then continue with the async generator."""
    for item in first_events:
        yield item
    async for item in agen:
        yield item


def build_seed_prompt(system: str, history: History, final_user: str | None) -> str:
    """Prompt for a NEW agy session: replay the conversation, then ask for a reply."""
    blocks: list[str] = []
    if system:
        blocks.append("[System instructions]\n" + system)
    for role, text in history:
        label = {"user": "User", "assistant": "Assistant", "tool": "Tool output"}.get(role, role.capitalize())
        blocks.append(f"[{label}]\n{text}")
    if final_user is not None:
        blocks.append("[User]\n" + final_user)
        blocks.append(
            "The conversation above is the recorded history. "
            "Reply to the most recent [User] message as the assistant, "
            "honoring any [System instructions]. Output only the reply itself."
        )
    else:
        blocks.append("Continue the conversation above as the assistant.")
    return "\n\n".join(blocks)


class DeltaTracker:
    """Streams agent_response text deltas across agy step_update events.

    text_delta is incremental in BOTH ACTIVE and DONE, including repeated text.
    A DONE transition does not imply a cumulative snapshot or a completed turn.
    """

    def __init__(self) -> None:
        self._emitted: dict[int, str] = {}

    def feed(self, step: dict) -> str | None:
        if step.get("step_type") != "agent_response":
            return None
        idx = step.get("step_index", 0)
        fragment = step.get("text_delta")
        if not isinstance(fragment, str) or not fragment:
            return None
        emitted = self._emitted.get(idx, "")
        self._emitted[idx] = emitted + fragment
        return fragment

    @property
    def streamed(self) -> str:
        return "".join(self._emitted.values())


async def record_dashboard_usage(
    cfg: Config,
    state,
    *,
    model: str | None,
    request,
    usage: dict | None,
    duration_ms: float,
    status: str = "SUCCESS",
) -> None:
    """Attribute one turn's agy usage to a dashboard log (never fails a turn)."""
    store = getattr(state, "store", None)
    if store is None:
        return
    if request is not None:
        if getattr(request.state, "usage_recorded", False):
            return
        request.state.usage_recorded = True
    try:
        key = api_key_record(cfg, request) if request is not None else None
        profile = getattr(request.state, "turn_profile", None) if request is not None else None
        await store.record_usage(
            model=model or "agy-default",
            key=key,
            usage=profile.usage if profile is not None else usage,
            duration_ms=duration_ms,
            status=status,
            diagnostics=profile.diagnostics() if profile is not None else None,
        )
    except Exception:  # pragma: no cover - accounting must not break chat
        log.warning("failed to record dashboard usage", exc_info=True)


@account_failover
async def run_turn(
    cfg: Config,
    mgr: SessionManager,
    *,
    system: str,
    history: History,
    final_user: str | None,
    model: str | None,
    effort: str | None,
    profile: TurnProfile | None = None,
    session_key: SessionIdentity | str | None = None,
) -> AsyncIterator[TurnEvent]:
    """Execute one agy turn, yielding ("delta", text) events then ("result", TurnResult)."""
    if profile is not None:
        profile.model = model
        profile.mark("turn_start")
    messages = [(r, t.strip() if r == "assistant" else t) for r, t in history]
    if final_user is not None:
        messages.append(("user", final_user))
    context = SessionContext.build(session_key, ["plain", system], messages)
    session = await mgr.acquire(model=model, effort=effort, context=context)
    if profile is not None:
        profile.new_session = session.turns == 0
        profile.mark("session_ready")
    if session.input_offset:
        prompt = build_seed_prompt("", messages[session.input_offset:], None)
    elif not history and not system and final_user is not None:
        prompt = final_user
    else:
        prompt = build_seed_prompt(system, history, final_user)

    tracker = DeltaTracker()
    completed = False
    reply_text: str | None = None
    events = session.send_turn(prompt)
    try:
        yield "ready", None
        async for event in events:
            if not isinstance(event, dict):
                continue
            kind = event.get("event")
            if kind == "init" and profile is not None:
                profile.mark("init_seen")
            if kind == "result":
                envelope = event.get("result")
                if not isinstance(envelope, dict):
                    envelope = {}
                status = envelope.get("status")
                if status != "SUCCESS":
                    raise AgyError(
                        envelope.get("error") or f"agy turn ended with status {status}",
                        status_code=502,
                    )
                text = envelope.get("response")
                if not isinstance(text, str):
                    structured = envelope.get("structured_output")
                    text = json.dumps_text(structured) if structured is not None else ""
                usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}

                streamed = tracker.streamed
                if text and not streamed:
                    if profile is not None:
                        profile.mark("first_delta")
                    yield ("delta", text)
                elif text.startswith(streamed) and text != streamed:
                    if profile is not None:
                        profile.mark("first_delta")
                    yield ("delta", text[len(streamed):])
                elif streamed and not text.startswith(streamed):
                    raise AgyError("agy result text does not match its streamed response")

                reply_text = text or streamed or ""
                completed = True
                if profile is not None:
                    profile.mark("result")
                yield (
                    "result",
                    TurnResult(
                        text=reply_text,
                        usage=usage,
                        status=status,
                        conversation_id=envelope.get("conversation_id") or session.conversation_id,
                        num_turns=envelope.get("num_turns"),
                    ),
                )
                return
            if kind == "step_update":
                delta = tracker.feed(event.get("step_update") or {})
                if delta:
                    if profile is not None:
                        profile.mark("first_delta")
                    yield ("delta", delta)
    finally:
        if profile is not None:
            log.info(
                "turn profile: %s status=%s",
                profile.summary(),
                "ok" if completed else "aborted",
            )
        try:
            await events.aclose()
        finally:
            await mgr.release(session, reply=("assistant", reply_text.strip()) if completed else None)
