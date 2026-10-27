"""Native token accounting uses upstream snapshots, never CLI totals."""
import asyncio
from contextlib import aclosing
from types import SimpleNamespace

import pytest

from tools.agy_native_wire_probe.usage import NativeUsage, normalize_usage


RAW = {"promptTokenCount": 1000, "cachedContentTokenCount": 600,
       "candidatesTokenCount": 25, "thoughtsTokenCount": 5, "totalTokenCount": 1030}
COUNTS = {"input_tokens": 400, "cache_read_tokens": 600, "output_tokens": 30,
          "thinking_tokens": 5, "total_tokens": 430}


def test_upstream_cache_and_thinking_preserve_internal_and_public_conventions():
    from app.token_usage import inclusive_counts

    assert normalize_usage(RAW) == COUNTS
    assert inclusive_counts(normalize_usage(RAW)) == (1000, 30, 1030)
    assert normalize_usage({"promptTokenCount": 10, "totalTokenCount": 10}) == {
        "input_tokens": 10, "cache_read_tokens": 0, "output_tokens": 0,
        "thinking_tokens": 0, "total_tokens": 10}
    # Explicit upstream zero is valid; missing usage is not zero consumption.
    assert normalize_usage({"promptTokenCount": 0, "totalTokenCount": 0}) == dict.fromkeys(COUNTS, 0)


@pytest.mark.parametrize("metadata", [
    None, {}, [], {"promptTokenCount": 10},
    dict(RAW, cachedContentTokenCount=1001), dict(RAW, totalTokenCount=1000),
    *(dict(RAW, candidatesTokenCount=value) for value in (-1, True, 1.5, "25", None)),
])
def test_unknown_or_invalid_metadata_is_not_fabricated(metadata):
    assert normalize_usage(metadata) is None


def test_snapshots_replace_chunks_and_sum_distinct_requests():
    usage = NativeUsage()
    usage.observe("first", {"promptTokenCount": 1000, "totalTokenCount": 1000})
    for _ in range(3):
        usage.observe("first", RAW, finished=True)
    usage.observe("second", RAW, finished=True)
    usage.observe("unrelated", RAW, finished=True)
    totals, complete = usage.snapshot({"first", "second"}, {"first", "second"})
    assert totals == {name: 2 * value for name, value in COUNTS.items()}
    assert complete
    assert not usage.snapshot({"first", "second", "missing"}, {"first", "second"})[1]


def test_partial_or_malformed_usage_retains_known_counts_but_is_incomplete():
    usage = NativeUsage()
    usage.observe("own", RAW)
    assert usage.snapshot({"own"}, {"own"}) == (COUNTS, False)
    usage.observe("own", None, finished=True)
    assert usage.snapshot({"own"}, {"own"}) == (COUNTS, False)
    # Gemini may send a usage-only chunk after the candidate finish marker.
    usage.observe("own", RAW)
    assert usage.snapshot({"own"}, {"own"}) == (COUNTS, True)
    usage.observe("own", dict(RAW, totalTokenCount="invalid"))
    assert usage.snapshot({"own"}, {"own"}) == (COUNTS, False)
    assert usage.snapshot(set(), set()) == ({}, False)


def test_cancellation_keeps_latest_native_usage_once(monkeypatch):
    from app.config import Config
    from app.external_tools import ExternalTools
    from app.native_chat import NativeAgySession, NativeSessionManager
    from app.runner import TurnProfile

    async def scenario():
        observed = asyncio.Event()

        class Observer:
            closed = False

            async def turn(self, prompt):
                yield {"kind": "usage", "usage": dict(COUNTS, output_tokens=0, total_tokens=400)}
                yield {"kind": "usage", "usage": COUNTS}
                observed.set()
                await asyncio.Event().wait()

            async def close(self):
                self.closed = True

        observer = Observer()

        async def start(session):
            session.observer = observer
            session.proc = SimpleNamespace(pid=123, returncode=None)

        monkeypatch.setattr(NativeAgySession, "start", start)
        manager = NativeSessionManager(Config())
        bridge = ExternalTools.from_body({"messages": [{"role": "user", "content": "test"}], "tools": []})
        profile = TurnProfile()

        async def consume():
            async with aclosing(bridge.run(manager, model="auto", effort=None, profile=profile)) as events:
                async for _ in events:
                    pass

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(observed.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert profile.usage == COUNTS
            assert not profile.usage_complete
            assert profile.error_code == "cancelled"
            assert observer.closed and manager.live_count == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await manager.close_all()

    asyncio.run(scenario())
