import asyncio
import json
from contextlib import aclosing
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.agy import AgyError
from app.config import Config, load_config
from app.native_chat import NativeAgySession, NativeSessionManager, captured_output


OUTPUT = {'kind': 'message', 'content': 'native-marker', 'tool_calls': []}


def test_native_binary_is_separate_from_regular_cli():
    cfg = Config(agy_bin='regular --v=2', native_bin='/opt/pinned/agy', recovery_attempts=1)
    manager = NativeSessionManager(cfg)
    assert manager.cfg.agy_bin == '/opt/pinned/agy'
    assert cfg.agy_bin == 'regular --v=2' and cfg.recovery_attempts == 1


def captured(output=None):
    output = OUTPUT if output is None else output
    return {'calls': [{'name': 'finish', 'args': dict(output, toolAction='internal', toolSummary='hidden')}],
            'cli': {'structured_output': output, 'status': 'SUCCESS', 'usage': {'input_tokens': 100}},
            'usage': {'input_tokens': 20}, 'metrics': {'agent_requests': 1}, 'kind': 'result'}


def test_capture_uses_only_native_content_and_strips_display_fields():
    result = captured()
    result['cli']['response'] = 'CLI stdout must never replace captured content'
    assert captured_output(result) == OUTPUT
    assert captured_output(result) is not result['cli']['structured_output']


@pytest.mark.parametrize('change', ['mismatch', 'missing', 'multiple', 'native_tool', 'malformed', 'extra'])
def test_invalid_capture_fails_closed(change):
    result = captured()
    if change == 'mismatch':
        result['cli']['structured_output'] = dict(OUTPUT, content='different')
    elif change == 'missing':
        result['calls'] = []
    elif change == 'multiple':
        result['calls'] *= 2
    elif change == 'native_tool':
        result['calls'][0]['name'] = 'run_command'
    elif change == 'malformed':
        result['calls'][0]['args'] = 'invalid'
    else:
        result['calls'][0]['args']['unknown'] = 'must not silently drop'
    with pytest.raises(AgyError):
        captured_output(result)


def test_native_chat_is_explicitly_opt_in(monkeypatch):
    monkeypatch.delenv('AGY_PROXY_NATIVE_CHAT', raising=False)
    assert not load_config().native_chat
    monkeypatch.setenv('AGY_PROXY_NATIVE_CHAT', '1')
    assert load_config().native_chat
    monkeypatch.delenv('AGY_PROXY_NATIVE_INPUT_CLEAN', raising=False)
    assert not load_config().native_input_clean
    monkeypatch.setenv('AGY_PROXY_NATIVE_INPUT_CLEAN', '1')
    assert load_config().native_input_clean


def test_chat_route_uses_native_manager_and_preserves_owner_isolation(tmp_path, monkeypatch):
    created = []

    class Observer:
        def __init__(self):
            self.turns = 0
            self.conversation_id = 'conversation-' + str(len(created))
            self.closed = False

        async def turn(self, prompt):
            self.turns += 1
            if self.turns == 1:
                yield {'kind': 'cli_init', 'init': {'agent': 'proxy-external'}, 'conversation_id': self.conversation_id}
            yield dict(captured(), cli=dict(captured()['cli'], conversation_id=self.conversation_id, num_turns=self.turns))

        async def close(self):
            self.closed = True

    async def start(session):
        created.append(session)
        session.observer = Observer()
        session.proc = SimpleNamespace(pid=1000+len(created), returncode=None)

    monkeypatch.setattr(NativeAgySession, 'start', start)
    fake = Path(__file__).with_name('fake_tools_agy.py').as_posix()
    cfg = Config(agy_bin=f'{sys.executable} {fake}', data_dir=str(tmp_path),
                 native_chat=True, api_keys=('alice', 'bob'), stream_heartbeat=.05)
    with TestClient(create_app(cfg)) as client:
        body = {'model': 'auto', 'messages': [{'role': 'user', 'content': 'first'}]}
        for owner in ('alice', 'bob'):
            response = client.post('/v1/chat/completions', json=body,
                                   headers={'Authorization': 'Bearer '+owner, 'X-Agy-Session-Id': 'shared'})
            assert response.status_code == 200, response.text
            assert response.json()['choices'][0]['message']['content'] == 'native-marker'
            assert response.json()['usage']['prompt_tokens'] == 20
        assert len(created) == 2
        assert created[0].conversation_id != created[1].conversation_id
        assert client.app.state.native_sessions.live_count == 2
        assert client.app.state.sessions.live_count == 0
        assert client.app.state.native_sessions.cfg.recovery_attempts == 1
        body['messages'] += [{'role': 'assistant', 'content': 'native-marker'}, {'role': 'user', 'content': 'next'}]
        body['stream'] = True
        response = client.post('/v1/chat/completions', json=body,
                               headers={'Authorization': 'Bearer alice', 'X-Agy-Session-Id': 'shared'})
        assert response.status_code == 200
        assert 'native-marker' in response.text and '[DONE]' in response.text
        assert len(created) == 2 and created[0].turns == 2
    assert all(session.observer.closed for session in created)


def test_closing_native_turn_closes_observer_generator():
    async def check():
        closed = []

        class Observer:
            async def turn(self, prompt):
                try:
                    yield {'kind': 'cli_step', 'step': {}}
                    await asyncio.Event().wait()
                finally:
                    closed.append(True)

        session = NativeAgySession(Config(), model='auto', effort=None)
        session.observer = Observer()
        async with aclosing(session.send_turn('x')) as events:
            await anext(events)
        assert closed and not session.lock.locked()
    asyncio.run(check())


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('failure', ['missing', 'persistent_missing', 'mismatch', 'multiple', 'wire'])
def test_native_repair_is_bounded_verified_and_never_delivers_rejected_output(tmp_path, monkeypatch, stream, failure):
    from tools.agy_native_wire_probe.runtime import WireError
    sessions, prompts = [], []

    class Observer:
        def __init__(self, index):
            self.index, self.turns, self.closed = index, 0, False
            self.conversation_id = 'native-' + str(index)

        async def turn(self, prompt):
            self.turns += 1
            prompts.append(prompt)
            yield {'kind': 'cli_init', 'init': {'agent': 'proxy-external'}, 'conversation_id': self.conversation_id}
            result = captured()
            if self.index == 0 or failure == 'persistent_missing':
                if failure in ('missing', 'persistent_missing'):
                    result['calls'] = []
                    result['text'] = 'REJECTED_PRIVATE_TEXT'
                    result['cli']['structured_output'] = None
                elif failure == 'mismatch':
                    result['cli']['structured_output'] = dict(OUTPUT, content='other')
                elif failure == 'multiple':
                    result['calls'] *= 2
                else:
                    raise WireError('synthetic capture failure')
            result['cli'].update(conversation_id=self.conversation_id, num_turns=1)
            yield result

        async def close(self):
            self.closed = True

    async def start(session):
        index = len(sessions)
        sessions.append(session)
        session.observer = Observer(index)
        session.proc = SimpleNamespace(pid=1000+index, returncode=None)

    monkeypatch.setattr(NativeAgySession, 'start', start)
    fake = Path(__file__).with_name('fake_tools_agy.py').as_posix()
    cfg = Config(agy_bin=f'{sys.executable} {fake}', data_dir=str(tmp_path), native_chat=True,
                 api_keys=('alice',), stream_heartbeat=.01)
    history = [{'role': 'user', 'content': 'ORIGINAL_CLIENT_HISTORY'},
               {'role': 'assistant', 'content': 'PRIOR_CONFIRMED_ANSWER'},
               {'role': 'user', 'content': 'CURRENT_REQUEST'}]
    with TestClient(create_app(cfg)) as client:
        response = client.post('/v1/chat/completions', json={'model': 'auto', 'messages': history, 'stream': stream},
                               headers={'Authorization': 'Bearer alice'})
        assert 'REJECTED_PRIVATE_TEXT' not in response.text
        expected_attempts = 2 if failure in ('missing', 'persistent_missing') else 1
        assert len(sessions) == expected_attempts
        assert all(isinstance(s, NativeAgySession) and s.observer.closed for s in sessions)
        assert client.app.state.native_sessions.live_count == 0
        record = list(client.app.state.store.requests)[-1].public()
        assert record['attempts'] == expected_attempts
        if failure == 'missing':
            assert response.status_code == 200
            assert response.text.count('native-marker') == 1
            assert record['repair_attempts'] == 1
            assert record['usage']['input_tokens'] == 40
            assert record['usage_complete'] is True
            assert all(marker in prompts[1] for marker in ('ORIGINAL_CLIENT_HISTORY', 'PRIOR_CONFIRMED_ANSWER', 'CURRENT_REQUEST'))
            assert 'required_submission' in prompts[1]
        elif stream:
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith('data: ') and line != 'data: [DONE]']
            assert any(e.get('error') for e in events)
            assert 'native-marker' not in response.text
        else:
            assert response.status_code == 502
