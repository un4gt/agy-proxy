"""Offline wire correlation tests. Run with: uv run --with hpack pytest ..."""
import importlib.util
import json
from pathlib import Path

import pytest

hpack = pytest.importorskip('hpack', reason='optional native wire probe dependency')


@pytest.fixture
def observer(tmp_path, monkeypatch):
    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('AGY_WIRE_EVENTS', str(path))
    monkeypatch.delenv('AGY_WIRE_INPUT_AUDIT', raising=False)
    monkeypatch.delenv('AGY_WIRE_INPUT_CLEAN', raising=False)
    spec = importlib.util.spec_from_file_location('test_observer', Path(__file__).parents[1] / 'tools/agy_native_wire_probe/observe.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.events = lambda: [json.loads(line) for line in path.read_text().splitlines()]
    yield module
    module.out.close()


def request(kind='agent'):
    return json.dumps({'requestType': kind, 'request': {}, 'model': 'test'}).encode()


def test_input_audit_is_opt_in_and_omits_outer_identifiers(observer, tmp_path, monkeypatch):
    root = tmp_path / 'audit'
    doc = {'requestType': 'agent', 'project': 'PRIVATE', 'requestId': 'PRIVATE',
           'request': {'contents': [], 'tools': [], 'safetySettings': ['unchanged']}}
    monkeypatch.delenv('AGY_WIRE_INPUT_AUDIT', raising=False)
    observer.audit_request(doc, '1')
    assert not root.exists()
    monkeypatch.setenv('AGY_WIRE_INPUT_AUDIT', str(root))
    observer.audit_request(dict(doc, requestType='checkpoint'), '2')
    assert not root.exists()
    observer.audit_request(doc, '3')
    assert json.loads(next(root.iterdir()).read_text()) == {'contents': [], 'tools': []}


@pytest.mark.parametrize('registered', [False, True])
def test_cleanup_requires_matching_serialized_request_at_tls_capture(observer, monkeypatch, registered):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'tools/agy_native_wire_probe'))
    from input_clean import digest
    monkeypatch.setenv('AGY_WIRE_INPUT_CLEAN', '1')
    body = request()
    if registered:
        observer.cleaned_requests.add(digest(json.loads(body)))
    observer.capture('test', 'send', b'POST /streamGenerateContent HTTP/1.1\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
    kinds = [e['kind'] for e in observer.events()]
    assert ('generation_request' in kinds) == registered
    assert ('request_parse_error' in kinds) != registered


def response(text):
    return json.dumps({'response': {'candidates': [{'content': {'parts': [{'text': text}]}, 'finishReason': 'STOP'}]}}).encode()


def quota_response():
    return {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED',
                     'message': 'PRIVATE upstream text must not leak',
                     'details': [{'reason': 'QUOTA_EXHAUSTED', 'metadata': {
                         'quotaResetTimeStamp': '2026-10-29T12:16:20Z', 'private': 'PRIVATE'}}]}}


@pytest.mark.parametrize('protocol', ['h1', 'h2'])
@pytest.mark.parametrize('mode', ['sse_header_json_body', 'json', 'gzip', 'sse_error', 'unparseable'])
def test_upstream_http_errors_are_not_silently_discarded(observer, protocol, mode):
    import gzip
    body = request()
    conn = 'upstream-error'
    if protocol == 'h1':
        observer.capture(conn, 'send', b'POST /streamGenerateContent HTTP/1.1\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\n\r\n' + body)
    else:
        observer.capture(conn, 'send', observer.H2_PREFACE)
        headers = hpack.Encoder().encode([(':method', 'POST'), (':path', '/streamGenerateContent')])
        observer.capture(conn, 'send', frame(1, 4, 1, headers) + frame(0, 1, 1, body))
    status = 200 if mode == 'sse_error' else 429
    body = json.dumps(quota_response()).encode()
    if mode == 'sse_error':
        body = b'data: ' + body + b'\n\n'
    elif mode == 'unparseable':
        body = b'PRIVATE non-JSON server error'
    headers = [('content-type', 'application/json' if mode == 'json' else 'text/event-stream')]
    if mode == 'gzip':
        body = gzip.compress(body)
        headers.append(('content-encoding', 'gzip'))
    if protocol == 'h1':
        packet = (f'HTTP/1.1 {status} Response\r\n'.encode() +
                  b''.join(f'{k}: {v}\r\n'.encode() for k, v in headers) +
                  f'Content-Length: {len(body)}\r\n\r\n'.encode() + body)
    else:
        encoded = hpack.Encoder().encode([(':status', str(status)), *headers])
        packet = frame(1, 4, 1, encoded) + frame(0, 1, 1, body)
    for at in range(0, len(packet), 11):
        observer.capture(conn, 'recv', packet[at:at + 11])
    events = observer.events()
    error, = [e for e in events if e['kind'] == 'upstream_error']
    generation, = [e for e in events if e['kind'] == 'generation_request']
    assert error['request_id'] == generation['request_id']
    assert error['status'] == 429
    assert error['quota_exhausted'] is (mode != 'unparseable')
    assert error['quota_reset_at'] == (None if mode == 'unparseable' else '2026-10-29T12:16:20Z')
    complete = next(e for e in events if e['kind'] == 'message_complete' and e['direction'] == 'recv')
    assert events.index(error) < events.index(complete)
    assert 'PRIVATE' not in json.dumps(events)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('account_count', [0, 1])
@pytest.mark.parametrize('status,quota', [(429, True), (429, False), (401, False), (503, False)])
def test_native_upstream_error_reaches_client_without_finish_repair(observer, tmp_path, monkeypatch, stream, account_count, status, quota):
    import sys
    import time
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from app import create_app
    from app.account_pool import AccountPool
    from app.config import Config
    from app.native_chat import NativeAgySession
    from tools.agy_native_wire_probe.runtime import NativeSession

    sessions = []

    async def start(session):
        native = NativeSession()
        sessions.append(session)
        session.observer = native
        session.proc = native.proc = SimpleNamespace(pid=1000, returncode=None)

        async def close():
            native.closed = True

        async def write(_):
            await native.queue.put(('stdout', {'event': 'init', 'observed_at': time.monotonic(),
                'conversation_id': 'quota-conversation', 'init': {'agent': 'proxy-external'}}))
            offset = len(observer.events())
            for conn, doc in [('side', {'error': {'code': 503}}), ('own', quota_response())]:
                if conn == 'own':
                    doc['error']['code'] = status
                    if not quota:
                        doc['error']['details'] = []
                body = request('checkpoint' if conn == 'side' else 'agent')
                observer.capture(conn, 'send', b'POST /streamGenerateContent HTTP/1.1\r\nContent-Length: '
                                 + str(len(body)).encode() + b'\r\n\r\n' + body)
                body = json.dumps(doc).encode()
                observer.capture(conn, 'recv', f'HTTP/1.1 {doc["error"]["code"]} Error\r\n'.encode() +
                                 b'Content-Type: text/event-stream\r\nContent-Length: ' +
                                 str(len(body)).encode() + b'\r\n\r\n' + body)
            # CLI stdout can incorrectly report SUCCESS and overtake the wire FIFO.
            await native.queue.put(('stdout', {'event': 'result', 'observed_at': time.monotonic(),
                'result': {'status': 'SUCCESS', 'conversation_id': 'quota-conversation',
                           'num_turns': 1, 'structured_output': None}}))
            for event in observer.events()[offset:]:
                await native.queue.put(('events', event))

        native.close, native._write = close, write

    monkeypatch.setattr(NativeAgySession, 'start', start)
    fake = Path(__file__).with_name('fake_tools_agy.py').as_posix()
    cfg = Config(agy_bin=f'{sys.executable} {fake}', native_chat=True,
                 data_dir=str(tmp_path / 'api-data'), api_keys=('test',))
    if account_count:
        cfg.account_pool = AccountPool(cfg)
        account = cfg.account_pool.create('fixture', {'token': {'access_token': 'test-access',
            'refresh_token': 'test-refresh', 'expiry': '2099-01-01T00:00:00Z'}, 'auth_method': 'consumer'})
    with TestClient(create_app(cfg)) as client:
        result = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer test'},
                             json={'model': 'auto', 'stream': stream,
                                   'messages': [{'role': 'user', 'content': 'inspect the image'}]})
        code = 'quota_exhausted' if quota else 'rate_limit_exceeded' if status == 429 else 'native_upstream_error'
        assert result.status_code == (200 if stream else status)
        if stream:
            events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
            error = next(e['error'] for e in events if 'error' in e)
        else:
            error = result.json()['error']
        assert error['code'] == code
        assert f'HTTP {status}' in error['message']
        if quota:
            assert '2026-10-29T12:16:20Z' in error['message']
        assert 'finish' not in error['message'] and 'PRIVATE' not in result.text
        record = client.app.state.store.requests[-1].public()
        assert record['error_code'] == code
        assert record['attempts'] == 1 and record['repair_attempts'] == 0
        if account_count:
            assert bool(cfg.account_pool.public(account.id)['exhausted_scopes']) is quota
    assert len(sessions) == 1 and sessions[0].observer.closed


def test_http1_keepalive_fragmentation_and_json_content_before_completion(observer):
    for kind, text in [('agent', 'FIRST'), ('checkpoint', 'SECOND')]:
        body = request(kind)
        observer.capture('connection', 'send', b'POST /streamGenerateContent HTTP/1.1\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
        body = response(text)
        packet = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body
        for offset in range(0, len(packet), 7):
            observer.capture('connection', 'recv', packet[offset:offset+7])
    events = observer.events()
    requests = [e for e in events if e['kind'] == 'generation_request']
    responses = [e for e in events if e['kind'] == 'json_response']
    assert [e['request_id'] for e in requests] == [e['request_id'] for e in responses]
    assert len({e['request_id'] for e in requests}) == 2
    for event in responses:
        completion = next(e for e in events if e['kind'] == 'message_complete' and e['direction'] == 'recv' and e['request_id'] == event['request_id'])
        assert events.index(event) < events.index(completion)
    assert not observer.requests


def frame(kind, flags, stream, data):
    return len(data).to_bytes(3, 'big') + bytes([kind, flags]) + stream.to_bytes(4, 'big') + data


def test_write_capture_uses_return_count_and_ignores_failed_writes(observer):
    body = request()
    packet = b'POST /streamGenerateContent HTTP/1.1\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body
    observer.capture_write('connection', b'not actually written', 0)
    observer.capture_write('connection', packet + b'not written either', len(packet))
    events = observer.events()
    assert len([e for e in events if e['kind'] == 'generation_request']) == 1
    assert [e['written'] for e in events if e['kind'] == 'short_write'] == [0, len(packet)]
    assert not observer.streams[('connection', 'send')].buffer
    with pytest.raises(ValueError):
        observer.capture_write('connection', b'x', 2)


def test_http2_interleaved_streams_keep_request_association(observer):
    encoder = hpack.Encoder()
    observer.capture('h2', 'send', b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n')
    for stream, kind in [(1, 'agent'), (3, 'checkpoint')]:
        headers = encoder.encode([(':method', 'POST'), (':path', '/streamGenerateContent')])
        observer.capture('h2', 'send', frame(1, 4, stream, headers) + frame(0, 1, stream, request(kind)))
    encoder = hpack.Encoder()
    for stream in (3, 1):
        headers = encoder.encode([(':status', '200'), ('content-type', 'text/event-stream')])
        observer.capture('h2', 'recv', frame(1, 4, stream, headers))
    for stream, text in [(1, 'OWN'), (3, 'AUX')]:
        payload = json.loads(response(text))
        payload['response']['usageMetadata'] = {'promptTokenCount': stream * 100,
                                               'totalTokenCount': stream * 100 + 2,
                                               'candidatesTokenCount': 2}
        observer.capture('h2', 'recv', frame(0, 1, stream, b'data: ' + json.dumps(payload).encode() + b'\n\n'))
    events = observer.events()
    requests = {e['request_type']: e['request_id'] for e in events if e['kind'] == 'generation_request'}
    outputs = {e['candidates'][0]['parts'][0]['text']: e['request_id'] for e in events if e['kind'] == 'sse'}
    assert outputs == {'OWN': requests['agent'], 'AUX': requests['checkpoint']}
    usage = {e['request_id']: e['usage'] for e in events if e['kind'] == 'sse'}
    assert usage[requests['agent']]['promptTokenCount'] == 100
    assert usage[requests['checkpoint']]['promptTokenCount'] == 300
    assert not observer.http2_paths and not observer.requests


def test_http2_end_stream_on_headers_waits_for_continuation(observer):
    observer.capture('h2', 'send', b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n')
    headers = hpack.Encoder().encode([(':method', 'GET'), (':path', '/health')])
    observer.capture('h2', 'send', frame(1, 1, 1, headers[:2]))
    assert not any(e['kind'] == 'message_complete' for e in observer.events())
    observer.capture('h2', 'send', frame(9, 4, 1, headers[2:]))
    assert len([e for e in observer.events() if e['kind'] == 'message_complete']) == 1


@pytest.mark.parametrize('fragment_size', [24, 7, 1])
def test_http2_new_connection_at_reused_address_has_fresh_parser(observer, fragment_size):
    for round_number in range(2):
        preface = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
        for offset in range(0, len(preface), fragment_size):
            observer.capture('reused-address', 'send', preface[offset:offset + fragment_size])
        headers = hpack.Encoder().encode([(':method', 'POST'), (':path', '/streamGenerateContent')])
        observer.capture('reused-address', 'send', frame(1, 4, 1, headers) + frame(0, 1, 1, request()))
        headers = hpack.Encoder().encode([(':status', '200'), ('content-type', 'text/event-stream')])
        observer.capture('reused-address', 'recv', frame(1, 4, 1, headers)
                         + frame(0, 1, 1, b'data: ' + response(f'ROUND-{round_number}') + b'\n\n'))
    events = observer.events()
    sent = [e for e in events if e['kind'] == 'generation_request']
    received = [e for e in events if e['kind'] == 'sse']
    assert len(sent) == len(received) == 2
    assert [e['request_id'] for e in sent] == [e['request_id'] for e in received]
    assert len({e['request_id'] for e in sent}) == 2
    assert not observer.http2_paths


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('upstream_stream', [False, True])
@pytest.mark.parametrize('mode', ['success', 'repair', 'repair_stale', 'capture_recover',
                                  'capture_failure', 'timeout', 'missing_usage'])
def test_parsed_wire_usage_reaches_api_and_store(observer, tmp_path, monkeypatch, stream, upstream_stream, mode):
    """Only process I/O is simulated: parse HTTP, run native validation and account."""
    import sys
    import time
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from app import create_app
    from app.config import Config
    from app.native_chat import NativeAgySession
    from tools.agy_native_wire_probe import runtime

    monkeypatch.setattr(runtime, 'CAPTURE_DRAIN_TIMEOUT', .01)
    output = {'kind': 'message', 'content': 'wire-usage-answer', 'tool_calls': []}
    raw = {'promptTokenCount': 1000, 'cachedContentTokenCount': 600,
           'candidatesTokenCount': 25, 'thoughtsTokenCount': 5, 'totalTokenCount': 1030}
    expected = {'input_tokens': 400, 'cache_read_tokens': 600, 'output_tokens': 30,
                'thinking_tokens': 5, 'total_tokens': 430}
    sessions = []

    async def start(session):
        index = len(sessions)
        sessions.append(session)
        native = runtime.NativeSession(timeout=.05, yield_cli_steps=True)
        session.observer = native
        session.proc = native.proc = SimpleNamespace(pid=1000 + index, returncode=None)

        async def close():
            native.closed = True

        async def write(_):
            number = native.turns
            conversation = f'usage-conversation-{index}'
            if number == 1:
                await native.queue.put(('stdout', {'event': 'init', 'observed_at': time.monotonic(),
                    'conversation_id': conversation, 'init': {'agent': 'proxy-external'}}))
            offset = len(observer.events())
            conn = f'usage-{index}-{number}'
            body = request()
            observer.capture(conn, 'send', b'POST /streamGenerateContent HTTP/1.1\r\nContent-Length: '
                             + str(len(body)).encode() + b'\r\n\r\n' + body)
            missing_finish = mode in ('repair', 'repair_stale') and index == 0
            parts = ([{'text': 'unsubmitted'}] if missing_finish else
                     [{'functionCall': {'name': 'finish', 'args': output}}])
            final = {'candidates': [{'content': {'parts': parts}, 'finishReason': 'STOP'}]}
            if mode != 'missing_usage':
                final['usageMetadata'] = raw
            if upstream_stream:
                chunks = []
                if mode != 'missing_usage':
                    chunks.append({'usageMetadata': dict(raw, candidatesTokenCount=0,
                                                         thoughtsTokenCount=0, totalTokenCount=1000)})
                chunks.append(final)
                if mode != 'missing_usage':
                    # A repeated final snapshot must not double the bill.
                    chunks.append({'usageMetadata': raw})
                body = b''.join(b'data: ' + json.dumps({'response': chunk}).encode() + b'\n\n' for chunk in chunks)
                content_type = b'text/event-stream'
            else:
                body = json.dumps({'response': final}).encode()
                content_type = b'application/json'
            packet = (b'HTTP/1.1 200 OK\r\nContent-Type: ' + content_type + b'\r\nContent-Length: '
                      + str(len(body)).encode() + b'\r\n\r\n' + body)
            for at in range(0, len(packet), 13):
                observer.capture(conn, 'recv', packet[at:at + 13])
            lost_capture = mode == 'capture_failure' or (mode == 'capture_recover' and index == 0)
            if not lost_capture:
                for event in observer.events()[offset:]:
                    await native.queue.put(('events', event))
            if mode != 'timeout':
                await native.queue.put(('stdout', {'event': 'result', 'observed_at': time.monotonic(),
                    'result': {'status': 'SUCCESS', 'conversation_id': conversation, 'num_turns': number,
                               'structured_output': None if mode == 'repair' and index == 0 else output,
                               'usage': {'input_tokens': 999999, 'output_tokens': -1}}}))

        native.close, native._write = close, write

    monkeypatch.setattr(NativeAgySession, 'start', start)
    fake = Path(__file__).with_name('fake_tools_agy.py').as_posix()
    cfg = Config(agy_bin=f'{sys.executable} {fake}', data_dir=str(tmp_path / 'data'),
                 native_chat=True, api_keys=('test',), stream_heartbeat=.01)
    body = {'model': 'auto', 'messages': [{'role': 'user', 'content': 'first'}],
            'stream': stream, 'stream_options': {'include_usage': True}}
    headers = {'Authorization': 'Bearer test', 'X-Agy-Session-Id': 'wire-usage'}

    with TestClient(create_app(cfg)) as client:
        for turn in range(2 if mode == 'success' else 1):
            response = client.post('/v1/chat/completions', json=body, headers=headers)
            record = client.app.state.store.requests[-1].public()
            multiplier = 2 if mode in ('repair', 'repair_stale') else 0 if mode in ('missing_usage', 'capture_failure') else 1
            assert record['usage'] == {name: value * multiplier for name, value in expected.items()}
            assert record['usage_complete'] is (mode in ('success', 'repair', 'repair_stale'))
            assert record['attempts'] == (2 if mode in ('repair', 'repair_stale', 'capture_recover', 'capture_failure') else 1)
            if mode in ('capture_failure', 'timeout'):
                assert record['error_code'] == ('native_capture_incomplete' if mode == 'capture_failure' else 'upstream_timeout')
                assert response.status_code == (200 if stream else 502 if mode == 'capture_failure' else 504)
                assert 'wire-usage-answer' not in response.text
                if stream:
                    assert 'data: [DONE]' not in response.text
                assert client.app.state.native_sessions.live_count == 0
                continue
            assert response.status_code == 200, response.text
            if stream:
                chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                          if line.startswith('data: ') and line != 'data: [DONE]']
                api_usage = next(chunk['usage'] for chunk in reversed(chunks) if chunk.get('usage'))
            else:
                api_usage = response.json()['usage']
            assert api_usage['prompt_tokens'] == 1000 * multiplier
            assert api_usage['completion_tokens'] == 30 * multiplier
            assert api_usage['total_tokens'] == 1030 * multiplier
            if multiplier:
                assert api_usage['prompt_tokens_details']['cached_tokens'] == 600 * multiplier
                assert api_usage['completion_tokens_details']['reasoning_tokens'] == 5 * multiplier
            body['messages'] += [{'role': 'assistant', 'content': 'wire-usage-answer'},
                                 {'role': 'user', 'content': f'next-{turn}'}]
        assert len(sessions) == (2 if mode in ('repair', 'repair_stale', 'capture_recover', 'capture_failure') else 1)
        if mode == 'capture_failure':
            # Both retries failed, but a later continuation must start clean.
            failed_sessions = list(sessions)
            assert all(s.observer.closed for s in failed_sessions)
            mode = 'success'
            body['messages'].append({'role': 'user', 'content': 'continue after capture failure'})
            continued = client.post('/v1/chat/completions', json=body, headers=headers)
            assert continued.status_code == 200 and 'wire-usage-answer' in continued.text
            assert len(sessions) == 3 and sessions[-1] not in failed_sessions
            record = client.app.state.store.requests[-1].public()
            assert record['usage'] == expected and record['usage_complete']
            assert record['attempts'] == 1
    assert all(session.observer.closed for session in sessions)


def test_close_discards_only_its_connection_and_hpack_state(observer):
    for conn in ('closed', 'survivor'):
        observer.capture(conn, 'send', observer.H2_PREFACE)
        headers = hpack.Encoder().encode([(':method', 'POST'), (':path', '/streamGenerateContent')])
        observer.capture(conn, 'send', frame(1, 4, 1, headers))
    observer.connection_closed('closed')
    assert ('closed', 'send') not in observer.streams
    assert 'closed' not in observer.http2_connections
    assert ('closed', 1) not in observer.http2_paths
    assert ('survivor', 1) in observer.http2_paths
    # The same address may now belong to an HTTP/1 connection.
    body = request()
    observer.capture('closed', 'send', b'POST /generateContent HTTP/1.1\r\nContent-Length: '
                     + str(len(body)).encode() + b'\r\n\r\n' + body)
    assert isinstance(observer.streams[('closed', 'send')], observer.Stream)
    assert any(e['kind'] == 'generation_request' and e['conn'] == 'closed' for e in observer.events())


def test_server_settings_can_arrive_before_fragmented_preface_finishes(observer):
    observer.capture('early', 'send', observer.H2_PREFACE[:5])
    observer.capture('early', 'recv', frame(4, 0, 0, b''))
    observer.capture('early', 'send', observer.H2_PREFACE[5:])
    assert isinstance(observer.streams[('early', 'recv')], observer.H2Stream)
    assert not observer.pending_receives and not observer.pending_prefaces
