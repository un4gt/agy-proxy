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
        observer.capture('h2', 'recv', frame(0, 1, stream, b'data: ' + response(text) + b'\n\n'))
    events = observer.events()
    requests = {e['request_type']: e['request_id'] for e in events if e['kind'] == 'generation_request'}
    outputs = {e['candidates'][0]['parts'][0]['text']: e['request_id'] for e in events if e['kind'] == 'sse'}
    assert outputs == {'OWN': requests['agent'], 'AUX': requests['checkpoint']}
    assert not observer.http2_paths and not observer.requests


def test_http2_end_stream_on_headers_waits_for_continuation(observer):
    observer.capture('h2', 'send', b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n')
    headers = hpack.Encoder().encode([(':method', 'GET'), (':path', '/health')])
    observer.capture('h2', 'send', frame(1, 1, 1, headers[:2]))
    assert not any(e['kind'] == 'message_complete' for e in observer.events())
    observer.capture('h2', 'send', frame(9, 4, 1, headers[2:]))
    assert len([e for e in observer.events() if e['kind'] == 'message_complete']) == 1
