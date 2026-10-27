import base64
import copy
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import create_app
from app.agy import AgyError
from app.config import Config
from app.native_chat import captured_output
import app.native_chat as native_chat
from tools.agy_native_wire_probe import runtime


OUTPUT = {'kind': 'message', 'content': 'two images inspected', 'tool_calls': []}


def image_url(color):
    out = io.BytesIO()
    Image.new('RGB', (3, 2), color).save(out, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode()


def result(calls):
    return {'kind': 'result', 'calls': calls, 'metrics': {'agent_requests': 2},
            'usage': {'input_tokens': 30, 'output_tokens': 10, 'total_tokens': 40},
            'usage_complete': True, 'cli': {'structured_output': OUTPUT, 'status': 'SUCCESS',
                                          'num_turns': 1, 'conversation_id': 'image-test'}}


def read_call(path):
    return {'name': 'view_file', 'args': {'AbsolutePath': str(path)}}


def finish_call():
    return {'name': 'finish', 'args': copy.deepcopy(OUTPUT)}


def test_native_capture_requires_allowed_reads_and_successful_completion(tmp_path):
    path = tmp_path / 'one.png'
    path.write_bytes(b'fixture')
    own = str(path.resolve())
    calls = [read_call(own), finish_call()]
    assert captured_output(result(calls), image_paths=[own], images_seen={own}) == OUTPUT
    # Already viewed images may be reused from the same session's history.
    assert captured_output(result([finish_call()]), image_paths=[own], images_seen={own}) == OUTPUT
    with pytest.raises(AgyError, match='confirmed successful'):
        captured_output(result(calls), image_paths=[own])
    with pytest.raises(AgyError, match='every supplied image'):
        captured_output(result([finish_call()]), image_paths=[own])
    with pytest.raises(AgyError, match='unapproved'):
        captured_output(result(calls))
    other = tmp_path / 'other.png'
    other.write_bytes(b'other owner')
    with pytest.raises(AgyError, match='unapproved'):
        captured_output(result([read_call(other.resolve()), finish_call()]), image_paths=[own], images_seen={own})
    calls[0]['args']['StartLine'] = 1
    with pytest.raises(AgyError, match='unapproved'):
        captured_output(result(calls), image_paths=[own], images_seen={own})
    with pytest.raises(AgyError, match='exactly one finish'):
        captured_output(result([finish_call(), finish_call()]), image_paths=[own], images_seen={own})


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('failure', [None, 'missing_read', 'read_failed', 'foreign_path', 'foreign_wire', 'mismatch'])
def test_native_image_api_validates_pixels_reads_and_final_capture(tmp_path, monkeypatch, stream, failure):
    observers = []

    class Observer:
        def __init__(self, **kwargs):
            self.settings = kwargs
            self.paths = kwargs['image_paths']
            self.turns = 0
            self.conversation_id = 'image-test'
            self.proc = SimpleNamespace(pid=1234, returncode=None)
            self.closed = False
            observers.append(self)

        async def start(self):
            assert self.settings['clean_input']
            assert len(self.paths) == 2
            root = Path(self.settings['cwd'])
            assert json.loads((root / 'image-permissions.json').read_text()) == self.paths
            assert 'tools: [finish, view_file]' in (root / '.agents/agents/proxy-external.md').read_text()
            assert [Image.open(path).getpixel((0, 0)) for path in self.paths] == [(255, 0, 0), (0, 0, 255)]

        async def turn(self, prompt):
            self.turns += 1
            assert all(json.dumps(path)[1:-1] in prompt for path in self.paths)
            yield {'kind': 'cli_init', 'init': {'agent': 'proxy-external'}, 'conversation_id': self.conversation_id}
            calls = []
            for i, path in enumerate(self.paths):
                call = read_call(path)
                if failure == 'foreign_path':
                    call = read_call('/unrelated/private.png')
                calls.append(call)
                if failure == 'missing_read' and i == 1:
                    continue
                info = {'name': 'view_file', 'parameters': call['args']}
                if failure == 'read_failed':
                    info['error'] = 'synthetic read failure'
                yield {'kind': 'cli_step', 'step': {'tool_info': info, 'state': 'DONE'}}
            if failure == 'foreign_wire':
                calls[0] = read_call('/unrelated/private.png')
            final = result(calls + [finish_call()])
            if failure == 'mismatch':
                final['cli']['structured_output'] = dict(OUTPUT, content='wrong')
            yield final

        async def close(self):
            self.closed = True

    monkeypatch.setattr(native_chat, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(native_chat, 'shutil', SimpleNamespace(which=lambda value: value))
    monkeypatch.setattr(runtime, 'NativeSession', Observer)
    fake = Path(__file__).with_name('fake_tools_agy.py').as_posix()
    cfg = Config(agy_bin=f'{sys.executable} {fake}', data_dir=str(tmp_path), native_chat=True,
                 native_input_clean=True, api_keys=('owner',), stream_heartbeat=.01)
    body = {'model': 'auto', 'stream': stream, 'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Read both images.'},
        *[{'type': 'image_url', 'image_url': {'url': image_url(c)}} for c in ('red', 'blue')]]}]}
    with TestClient(create_app(cfg)) as client:
        response = client.post('/v1/chat/completions', json=body, headers={'Authorization': 'Bearer owner'})
        assert len(observers) == 1
        if failure is None:
            assert response.status_code == 200, response.text
            assert OUTPUT['content'] in response.text
            assert '[DONE]' in response.text if stream else response.json()['usage']['prompt_tokens'] == 30
        else:
            assert 'error' in response.text
            assert OUTPUT['content'] not in response.text
            assert response.status_code == (200 if stream else 502)
        assert 'Native Chat image support is not yet verified' not in response.text
        assert all(path not in response.text for path in observers[0].paths)
    assert all(o.closed and all(not Path(path).exists() for path in o.paths) for o in observers)
