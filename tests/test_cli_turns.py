"""CLI-injected finish reminders are input turns, not new client requests."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app.agy import AgyError, AgySession
from tools.agy_cli_protocol import CliTurnCounter
from app.config import Config
from app.native_chat import NativeAgySession
from tools.agy_native_wire_probe.runtime import NativeSession, WireError


OUTPUT = {"kind": "message", "content": "verified", "tool_calls": []}


def session_with_events(backend, *, result_count=2, step_conversation="conversation", wire_last=False):
    session = (NativeAgySession if backend == "native" else AgySession)(Config(), model="auto", effort=None)
    observer = NativeSession(yield_cli_steps=True)
    observer.proc = SimpleNamespace(returncode=None)
    submissions = []

    async def close():
        observer.closed = True

    observer.close = close

    async def submit(_):
        submissions.append(True)
        number = len(submissions)
        steps = [0, 2] if number == 1 else [5]
        cli = []
        if number == 1:
            cli.append({"event": "init", "conversation_id": "conversation", "init": {"agent": "proxy-external"}})
        for index in steps:
            # Repeated lifecycle updates must not double-count a user input.
            for state in ("ACTIVE", "DONE", "DONE"):
                cli.append({"event": "step_update", "step_update": {
                    "conversation_id": step_conversation, "step_type": "user_input",
                    "step_index": index, "state": state}})
        cli.append({"event": "result", "result": {
            "status": "SUCCESS", "conversation_id": "conversation",
            "num_turns": result_count if number == 1 else 3,
            "structured_output": OUTPUT, "usage": {"input_tokens": 100 * number}}})
        if backend == "headless":
            for event in cli:
                await session.events.put(event)
            return
        wire = []
        for index in steps:
            request_id = str(index)
            parts = ([{"functionCall": {"name": "finish", "args": OUTPUT}}]
                     if index == steps[-1] else [{"text": "unsubmitted draft"}])
            for event in [
                {"kind": "generation_request", "request_id": request_id, "request_type": "agent"},
                {"kind": "sse", "request_id": request_id, "candidates": [
                    {"index": 0, "finishReason": "STOP", "parts": parts}],
                 "usage": {"promptTokenCount": 100 // len(steps), "totalTokenCount": 100 // len(steps)}},
                {"kind": "message_complete", "request_id": request_id, "direction": "recv"},
            ]:
                wire.append(("events", dict(event, monotonic=time.monotonic())))
        stdout = [("stdout", dict(event, observed_at=time.monotonic())) for event in cli]
        for row in stdout + wire if wire_last else wire + stdout:
            await observer.queue.put(row)

    async def drain():
        await submit(None)

    session.proc = SimpleNamespace(pid=123, returncode=None,
                                   stdin=SimpleNamespace(write=lambda _: None, drain=drain))
    if backend == "native":
        observer._write = submit
        session.observer = observer
    return session, submissions


@pytest.mark.parametrize("backend", ["headless", "native"])
@pytest.mark.parametrize("wire_last", [False, True])
def test_internal_reminder_then_next_client_turn_preserves_usage_and_session(backend, wire_last):
    async def check():
        session, submissions = session_with_events(backend, wire_last=wire_last)
        results = []
        for _ in range(2):
            events = [event async for event in session.send_turn("client input")]
            results.append(events[-1]["result"])
        assert len(submissions) == 2
        assert session.turns == 2
        assert [result["num_turns"] for result in results] == [2, 3]
        expected_usage = {"input_tokens": 100}
        if backend == "native":
            expected_usage.update(output_tokens=0, thinking_tokens=0, cache_read_tokens=0, total_tokens=100)
            assert all(result["usage_complete"] for result in results)
        assert all(result["usage"] == expected_usage for result in results)
        assert all(result["structured_output"] == OUTPUT for result in results)
        if backend == "native":
            assert not session.observer.closed
            assert session.observer.last_metrics["cli_turns"] == 3
    asyncio.run(check())


@pytest.mark.parametrize("backend", ["headless", "native"])
@pytest.mark.parametrize("count", [None, True, 2.0, "2", 0, 1, 3, 99])
def test_extra_cli_turns_require_matching_observed_inputs(backend, count):
    async def check():
        session, _ = session_with_events(backend, result_count=count)
        with pytest.raises(AgyError, match="CLI turn number mismatch"):
            async for _ in session.send_turn("client input"):
                pass
        if backend == "native":
            assert session.observer.closed
    asyncio.run(check())


@pytest.mark.parametrize("backend", ["headless", "native"])
def test_foreign_conversation_step_cannot_justify_an_extra_turn(backend):
    async def check():
        session, _ = session_with_events(backend, step_conversation="another-owner")
        with pytest.raises(AgyError, match="another conversation"):
            async for _ in session.send_turn("client input"):
                pass
    asyncio.run(check())


def test_completed_user_steps_cannot_be_replayed_as_a_new_turn():
    counter = CliTurnCounter()
    counter.begin()
    counter.observe({"step_type": "user_input", "step_index": 0})
    counter.observe({"step_type": "user_input", "step_index": 2})
    counter.confirm(2)
    counter.begin()
    with pytest.raises(ValueError, match="completed turn"):
        counter.observe({"step_type": "user_input", "step_index": 2})
    with pytest.raises(ValueError, match="turn number mismatch"):
        counter.confirm(2)


@pytest.mark.parametrize("index", [None, True, 1.0, "1", -1])
def test_invalid_input_step_cannot_increase_expected_turns(index):
    counter = CliTurnCounter()
    counter.begin()
    with pytest.raises(ValueError, match="invalid step index"):
        counter.observe({"step_type": "user_input", "step_index": index})


def test_finished_cli_with_lost_capture_fails_without_waiting_for_model_timeout(monkeypatch):
    import tools.agy_native_wire_probe.runtime as runtime
    monkeypatch.setattr(runtime, "CAPTURE_DRAIN_TIMEOUT", 0.01)

    async def check():
        session = NativeSession(timeout=600)
        session.proc = SimpleNamespace(returncode=None)
        session.conversation_id = "conversation"

        async def close():
            session.closed = True

        async def write(_):
            await session.queue.put(("stdout", {"event": "result", "observed_at": time.monotonic(),
                "result": {"status": "SUCCESS", "conversation_id": "conversation", "num_turns": 1,
                           "structured_output": OUTPUT}}))

        session.close, session._write = close, write
        async with asyncio.timeout(1):
            with pytest.raises(WireError, match="Incomplete native capture after CLI result") as caught:
                async for _ in session.turn("fixture"):
                    pass
        assert caught.value.code == "native_capture_incomplete"
        assert session.closed
    asyncio.run(check())
