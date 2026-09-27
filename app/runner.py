"""Shared turn executor used by both the Gemini and OpenAI routers.

Flattens a full client-side conversation into one agy turn, reusing a warm agy
session whenever the incoming history matches that session's history prefix
(agy keeps native multi-turn context inside the process).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

from .agy import AgyError, SessionManager
from .auth import api_key_record
from .config import Config

log = logging.getLogger("agy_proxy.runner")


@dataclass
class TurnProfile:
    """Per-request phase timing, logged once the turn finishes."""

    started: float = field(default_factory=time.monotonic)
    model: str | None = None
    new_session: bool = False
    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, time.monotonic() - self.started)

    @property
    def total_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000

    def summary(self) -> str:
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
            return (end - start) * 1000

        parts = [
            f"model={self.model}",
            f"new_session={int(self.new_session)}",
            f"models_lookup={ms('models_done', 'turn_start'):.0f}ms",
            f"acquire={ms('turn_start', 'session_ready'):.0f}ms",
            f"agy_boot={ms('session_ready', 'init_seen'):.0f}ms",
            f"think={ms('init_seen' if self.marks.get('init_seen') is not None else 'session_ready', 'first_delta'):.0f}ms",
            f"stream={ms('first_delta', 'result'):.0f}ms",
            f"total={self.total_ms:.0f}ms",
        ]
        return " ".join(parts)

# history entries are (role, text) tuples; role is "user" | "assistant" | "tool"
History = list[tuple[str, str]]

TurnEvent = tuple[Literal["delta", "result"], object]


@dataclass
class TurnResult:
    text: str
    usage: dict = field(default_factory=dict)
    status: str = "SUCCESS"
    conversation_id: str | None = None
    num_turns: int | None = None


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

    ACTIVE events carry incremental fragments; the DONE event of a step carries
    the full cumulative text of that step, so only the not-yet-emitted remainder
    is forwarded on DONE.
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
        if step.get("state") == "DONE":
            if not emitted:
                delta = fragment
            elif fragment == emitted:
                return None
            elif fragment.startswith(emitted):
                delta = fragment[len(emitted):]
            else:
                log.warning("agy DONE text diverges from streamed fragments for step %s; keeping fragments", idx)
                return None
            self._emitted[idx] = fragment
            return delta or None
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
    try:
        key = api_key_record(cfg, request) if request is not None else None
        await store.record_usage(
            model=model or "agy-default",
            key=key,
            usage=usage,
            duration_ms=duration_ms,
            status=status,
        )
    except Exception:  # pragma: no cover - accounting must not break chat
        log.warning("failed to record dashboard usage", exc_info=True)


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
) -> AsyncIterator[TurnEvent]:
    """Execute one agy turn, yielding ("delta", text) events then ("result", TurnResult)."""
    if profile is not None:
        profile.model = model
        profile.mark("turn_start")
    before = mgr.key(model, effort, system, history)
    full_history: History = list(history)
    if final_user is not None:
        full_history.append(("user", final_user))

    session, is_new = await mgr.acquire(before, model=model, effort=effort)
    if profile is not None:
        profile.new_session = is_new
        profile.mark("session_ready")
    if is_new:
        # With no prior history and no system prompt there is nothing to
        # replay: send the user message as-is so the model sees exactly what
        # the client sent.
        if not history and not system and final_user is not None:
            prompt = final_user
        else:
            prompt = build_seed_prompt(system, history, final_user)
    else:
        prompt = final_user if final_user is not None else "(Continue the conversation.)"

    tracker = DeltaTracker()
    completed = False
    reply_text: str | None = None
    try:
        async for event in session.send_turn(prompt):
            if not isinstance(event, dict):
                continue
            kind = event.get("event")
            if kind == "init" and profile is not None:
                profile.mark("init_seen")
            if kind == "result":
                envelope = event.get("result")
                if not isinstance(envelope, dict):
                    envelope = {}
                status = envelope.get("status") or "SUCCESS"
                if status != "SUCCESS":
                    raise AgyError(
                        envelope.get("error") or f"agy turn ended with status {status}",
                        status_code=502,
                    )
                text = envelope.get("response")
                if not isinstance(text, str):
                    structured = envelope.get("structured_output")
                    text = json.dumps(structured, ensure_ascii=False) if structured is not None else ""
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
                    log.warning("agy result text diverges from streamed deltas; keeping streamed output")

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
        if completed:
            # The client will echo the assistant reply back in its next
            # request, so the reusable key must include it.
            after = mgr.key(model, effort, system, full_history + [("assistant", reply_text or "")])
            mgr.rekey(before, after, session)
        else:
            # Aborted turn (client disconnect, timeout, error): the agy-side
            # conversation is in an unknown state — drop the session.
            await mgr.drop(session)
