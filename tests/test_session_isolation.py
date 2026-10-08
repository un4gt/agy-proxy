"""Request ownership, capacity and cancellation regressions using real child processes."""

import asyncio
import json
import sys
from contextlib import aclosing
from pathlib import Path

import pytest

from app.agy import AgyError, AgySession, SessionManager
from app.config import Config
from app.runner import run_turn


def config(**kwargs):
    fake = (Path(__file__).parent / 'fake_agy.py').as_posix()
    return Config(agy_bin=f'{sys.executable} {fake}', **kwargs)


def test_parallel_requests_never_exceed_capacity_or_steal_a_process():
    async def scenario():
        mgr = SessionManager(config(max_sessions=2))
        try:
            acquired = await asyncio.gather(*[
                mgr.acquire(model='auto', effort=None) for _ in range(8)
            ], return_exceptions=True)
            sessions = [r for r in acquired if isinstance(r, AgySession)]
            errors = [r for r in acquired if isinstance(r, AgyError)]
            assert len(sessions) == 2
            assert len(errors) == 6 and all(e.status_code == 429 for e in errors)
            assert sessions[0] is not sessions[1]
            assert sessions[0].proc.pid != sessions[1].proc.pid
            assert mgr.live_count == mgr.busy_count() == 2
            # Neither an update nor the sweeper may kill a reserved process,
            # even before it begins reading its first turn.
            assert await mgr.close_idle() == 0
            await mgr.sweep()
            for session in sessions:
                async with aclosing(session.send_turn('PING')) as events:
                    result = [e async for e in events][-1]['result']
                assert result['response'] == 'pong'
                assert result['num_turns'] == 1
            assert mgr.busy_count() == 2  # ownership outlives the pipe lock
        finally:
            await mgr.close_all()
        assert mgr.live_count == 0
    asyncio.run(scenario())


def test_prewarm_is_claimed_once_and_never_recycled_after_use():
    async def scenario():
        mgr = SessionManager(config(max_sessions=1, prewarm_models=('auto', 'another-model')))
        try:
            await mgr.ensure_prewarm()
            assert mgr.live_count == 1
            session = await mgr.acquire(model='auto', effort=None)
            await mgr.ensure_prewarm()
            with pytest.raises(AgyError) as caught:
                await mgr.acquire(model='auto', effort=None)
            assert caught.value.status_code == 429
            async with aclosing(session.send_turn('PING')) as events:
                assert [e async for e in events][-1]['result']['response'] == 'pong'
            await mgr.drop(session)
            assert not session.alive
            next_session = await mgr.acquire(model='auto', effort=None)
            assert next_session is not session and next_session.turns == 0
        finally:
            await mgr.close_all()
    asyncio.run(scenario())


def test_identical_forked_histories_use_separate_upstream_conversations():
    async def scenario():
        cfg = config(max_sessions=2)
        mgr = SessionManager(cfg)
        async def turn(history, user):
            async with aclosing(run_turn(cfg, mgr, system='', history=history,
                                         final_user=user, model='auto', effort=None)) as events:
                return [v async for kind, v in events if kind == 'result'][0]
        try:
            first = await turn([], 'COMMON')
            shared = [('user', 'COMMON'), ('assistant', first.text)]
            left, right = await asyncio.gather(turn(shared, 'ONLY_LEFT'), turn(shared, 'ONLY_RIGHT'))
            assert len({first.conversation_id, left.conversation_id, right.conversation_id}) == 3
            for result, own, other in [(left, 'ONLY_LEFT', 'ONLY_RIGHT'), (right, 'ONLY_RIGHT', 'ONLY_LEFT')]:
                assert result.num_turns == 1
                assert 'COMMON' in result.text and own in result.text and other not in result.text
            assert mgr.live_count == 0
        finally:
            await mgr.close_all()
    asyncio.run(scenario())


def test_cancelled_turn_releases_its_slot_and_process():
    async def scenario():
        cfg = config(max_sessions=1)
        mgr = SessionManager(cfg)
        try:
            turn = run_turn(cfg, mgr, system='', history=[], final_user='cancel me',
                            model='auto', effort=None)
            assert (await anext(turn))[0] == 'delta'
            assert mgr.busy_count() == 1
            await turn.aclose()
            assert mgr.live_count == 0
            replacement = await mgr.acquire(model='auto', effort=None)
            assert replacement.alive and replacement.turns == 0
        finally:
            await mgr.close_all()
    asyncio.run(scenario())


def test_cancelled_start_does_not_leak_process_or_capacity(monkeypatch):
    async def scenario():
        cfg = config(max_sessions=1)
        mgr = SessionManager(cfg)
        started = asyncio.Event()
        original = AgySession.start
        children = []
        async def blocked_start(session):
            await original(session)
            children.append(session)
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(AgySession, 'start', blocked_start)
        task = asyncio.create_task(mgr.acquire(model='auto', effort=None))
        try:
            await asyncio.wait_for(started.wait(), 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not children[0].alive and mgr.live_count == 0
            monkeypatch.setattr(AgySession, 'start', original)
            assert (await mgr.acquire(model='auto', effort=None)).alive
        finally:
            task.cancel()
            await mgr.close_all()
    asyncio.run(scenario())


@pytest.mark.parametrize('api', ['openai', 'gemini'])
def test_closing_http_stream_immediately_releases_request(api, tmp_path):
    from starlette.requests import Request
    from app import create_app
    from app.openai_api import chat_completions
    from app.gemini_api import gemini_action

    async def scenario():
        app = create_app(config(data_dir=str(tmp_path), max_sessions=1))
        body = ({'model': 'auto', 'stream': True, 'messages': [{'role':'user', 'content':'hello'}]}
                if api == 'openai' else {'contents': [{'role':'user', 'parts':[{'text':'hello'}]}]})
        async def receive():
            return {'type':'http.request', 'body':json.dumps(body).encode(), 'more_body':False}
        request = Request({'type':'http', 'method':'POST', 'path':'/', 'headers':[], 'app':app,
                           'query_string':b''}, receive=receive)
        try:
            response = (await chat_completions(request) if api == 'openai'
                        else await gemini_action('models/auto:streamGenerateContent', request))
            assert response.status_code == 200
            assert app.state.sessions.busy_count() == 1
            await anext(response.body_iterator)
            await response.body_iterator.aclose()
            assert app.state.sessions.live_count == 0
        finally:
            await app.state.sessions.close_all()
    asyncio.run(scenario())
