"""Isolation checks for the opt-in prototype. No real CLI or network calls."""
import asyncio
from contextlib import aclosing
from types import SimpleNamespace
import time

import pytest

from tools.agy_native_wire_probe.runtime import NativeSession, WireError
from tools.agy_native_wire_probe.sessions import NativeSessions


class FakeSession:
    def __init__(self, **configuration):
        self.closed = False
        self.configuration = configuration
        self.started = False

    async def start(self):
        self.started = True
        if self.configuration.get('fail'):
            raise WireError('startup failure')

    async def close(self):
        self.closed = True


def test_owner_and_session_namespaces_and_configuration():
    async def check():
        pool = NativeSessions(factory=FakeSession)
        async with pool.lease('alice', 'shared', model='one') as a:
            async with pool.lease('bob', 'shared', model='one') as b:
                async with pool.lease('alice', 'other', model='one') as c:
                    assert len({id(a), id(b), id(c)}) == 3
                    with pytest.raises(WireError, match='active turn'):
                        async with pool.lease('alice', 'shared', model='one'):
                            pytest.fail('busy session reused')
        async with pool.lease('alice', 'shared', model='one') as again:
            assert again is a
        async with pool.lease('alice', 'shared', model='two') as changed:
            assert changed is not a and a.closed
        assert not b.closed and not c.closed
        await pool.close()
        assert b.closed and c.closed and changed.closed
    asyncio.run(check())


def test_cancel_one_lease_preserves_other_and_cleans_failed_start():
    async def check():
        pool = NativeSessions(factory=FakeSession)
        active = asyncio.Event()
        victim = []

        async def work():
            async with pool.lease('alice', 'same') as session:
                victim.append(session)
                active.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(work())
        await active.wait()
        async with pool.lease('bob', 'same') as survivor:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert victim[0].closed and not survivor.closed
            assert pool.key('alice', 'same') not in pool.entries
        with pytest.raises(WireError, match='startup failure'):
            async with pool.lease('alice', 'failed', fail=True):
                pass
        assert pool.key('alice', 'failed') not in pool.entries
        async with pool.lease('bob', 'same') as again:
            assert again is survivor
        await pool.close()
    asyncio.run(check())


def test_reservation_is_visible_while_starting_and_capacity_is_bounded():
    async def check():
        starting, resume = asyncio.Event(), asyncio.Event()

        class Slow(FakeSession):
            async def start(self):
                starting.set()
                await resume.wait()

        pool = NativeSessions(factory=Slow, limit=1)

        async def work():
            async with pool.lease('alice', 'same'):
                pass

        task = asyncio.create_task(work())
        await starting.wait()
        with pytest.raises(WireError, match='active turn'):
            async with pool.lease('alice', 'same'):
                pass
        with pytest.raises(WireError, match='capacity'):
            async with pool.lease('bob', 'same'):
                pass
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not pool.entries
        await pool.close()
    asyncio.run(check())


def test_request_correlation_ignores_other_requests_and_waits_for_both_channels():
    async def check():
        session = NativeSession()
        session.proc = SimpleNamespace(returncode=None)
        session.conversation_id = 'local-conversation'
        now = time.monotonic()

        async def write(_):
            def event(kind, request_id, **fields):
                return ('events', dict(kind=kind, request_id=request_id, monotonic=time.monotonic(), **fields))
            def content(text):
                return {'candidates': [{'index': 0, 'finishReason': 'STOP', 'parts': [{'text': text}]}]}
            rows = [
                ('events', {'kind': 'generation_request', 'request_id': 'old', 'request_type': 'agent', 'monotonic': now-10}),
                event('generation_request', 'side', request_type='checkpoint'),
                event('generation_request', 'own', request_type='agent'),
                event('sse', 'side', **content('DO_NOT_LEAK')),
                event('sse', 'old', **content('STALE')),
                event('sse', 'own', **content('EXPECTED')),
                ('stdout', {'event': 'result', 'observed_at': time.monotonic(), 'result': {
                    'status': 'SUCCESS', 'conversation_id': 'local-conversation', 'num_turns': 1, 'usage': {'input_tokens': 10}}}),
                event('message_complete', 'own', direction='recv'),
            ]
            for row in rows:
                await session.queue.put(row)

        session._write = write
        async with aclosing(session.turn('test')) as events:
            # HTTP heartbeat adapters can pull successive items in different
            # tasks; a timeout scope must not remain open across a yield.
            first = await asyncio.create_task(anext(events))
            assert first['kind'] == 'text' and first['text'] == 'EXPECTED'
            # Text is already observable before the terminal CLI event is consumed.
            assert session.queue.qsize() == 2
            result = await asyncio.create_task(anext(events))
            assert result['kind'] == 'result' and result['text'] == 'EXPECTED'
            assert result['usage'] == {'input_tokens': 10}
        assert session.queue.empty()
    asyncio.run(check())
