"""Shared turn types and accounting for the native bridge.

The bridge owns streaming and tool translation; this module keeps the
protocol-neutral result/profile types plus the dashboard usage hook used by
the OpenAI, Gemini, Messages and Responses adapters.
"""

from __future__ import annotations

from . import json_codec as json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

from .auth import api_key_record
from .config import Config
from .contracts import ExecutionOutcome
from .latency import LatencyTrace

log = logging.getLogger("agy_proxy.runner")


@dataclass
class TurnProfile:
    """Per-request phase timing, logged once the turn finishes."""

    started: float = field(default_factory=time.monotonic)
    model: str | None = None
    account_id: str | None = None
    account_name: str = "—"
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


TurnEvent = tuple[Literal["ready", "delta", "result"], object]


@dataclass
class TurnResult(ExecutionOutcome):
    status: str = "SUCCESS"
    stop_reason: str | None = None

    @property
    def tool_calls(self):
        return [{"id": call.id, "type": "function", "function": {
            "name": call.name, "arguments": json.dumps_text(call.arguments, sort_keys=True)}} for call in self.calls]

    @property
    def finish_reason(self) -> str:
        if self.tool_calls:
            return "tool_calls"
        return self.stop_reason or "stop"

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
            account_id=profile.account_id if profile is not None else None,
            account_name=profile.account_name if profile is not None else "—",
            diagnostics=profile.diagnostics() if profile is not None else None,
        )
    except Exception:  # pragma: no cover - accounting must not break chat
        log.warning("failed to record dashboard usage", exc_info=True)
