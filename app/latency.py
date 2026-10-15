"""Bounded wall-clock observations; never collect prompts, arguments or output."""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Span:
    trace: LatencyTrace
    name: str
    start_ms: float
    args: dict
    duration_ms: float | None = None
    outcome: str = "ok"

    def end(self):
        if self.duration_ms is None:
            self.duration_ms = max(0, self.trace.elapsed_ms() - self.start_ms)

    def public(self):
        return {"name": self.name, "start_ms": round(self.start_ms, 3),
                "duration_ms": round(self.duration_ms, 3) if self.duration_ms is not None else None,
                "outcome": self.outcome, **self.args}


@dataclass
class LatencyTrace:
    # One instance is explicitly shared through request.state. No task-local
    # context is entered across async-generator yields (heartbeat task changes).
    # perf_counter is monotonic and also high resolution on Python 3.12/Windows,
    # where monotonic() may otherwise quantize short spans to ~15 ms.
    started: float = field(default_factory=time.perf_counter)
    started_unix_us: int = field(default_factory=lambda: time.time_ns() // 1000)
    spans: list[Span] = field(default_factory=list)
    marks: dict[str, float] = field(default_factory=dict)
    dropped: int = 0
    limit: int = 64

    def elapsed_ms(self):
        return (time.perf_counter() - self.started) * 1000

    def mark(self, name: str):
        if name in self.marks:
            return
        if len(self.marks) >= self.limit:
            self.dropped += 1
            return
        self.marks[name] = round(self.elapsed_ms(), 3)

    @contextmanager
    def span(self, name: str, **metadata):
        span = Span(self, name, self.elapsed_ms(), metadata)
        if len(self.spans) < self.limit:
            self.spans.append(span)
        else:
            self.dropped += 1
        try:
            yield span
        except BaseException as exc:
            if span.duration_ms is None:
                span.outcome = "cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "error"
            # No exception message: it may contain a candidate or client data.
            raise
        finally:
            span.end()

    def public(self):
        return {"started_unix_us": self.started_unix_us,
                "total_ms": round(self.elapsed_ms(), 3),
                "marks_ms": dict(self.marks), "spans": [s.public() for s in self.spans],
                "dropped_observations": self.dropped}
