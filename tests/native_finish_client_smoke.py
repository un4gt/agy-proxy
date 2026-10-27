"""Real pinned CLI + native observer + Chat API, with an offline TLS upstream.

Run in a Linux container with --network none, SYS_PTRACE and unconfined seccomp.
No real credentials or upstream are used. Unlike native_finish_boundary_smoke,
this checks what a coding client receives, including tools and session reuse.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
MODEL = 'gemini-3.8-flash-low'
TOOL = {'type': 'function', 'function': {
    'name': 'read_fixture', 'description': 'Read on the client only',
    'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}},
                   'required': ['path'], 'additionalProperties': False}}}


def submission(content='', path=None):
    return {'functionCall': {'name': 'finish', 'args': {
        'kind': 'tool_calls' if path else 'message', 'content': content,
        'tool_calls': [{'name': 'read_fixture', 'arguments': {'path': path}}] if path else [],
        'toolAction': 'respond', 'toolSummary': 'Offline fixture'}}}


class Upstream:
    def __init__(self, root):
        self.requests = []
        self.responses = [
            submission(path='client-one.txt'),
            {'text': 'UNSUBMITTED_PRIVATE_2'}, submission('CLIENT_RESULT_ONE'),
            {'text': 'UNSUBMITTED_PRIVATE_3'}, submission(path='client-two.txt'),
            {'text': 'UNSUBMITTED_PRIVATE_4'}, submission('CLIENT_RESULT_TWO'),
            {'text': 'UNSUBMITTED_PRIVATE_5'}, submission('FINAL_CURRENT_TURN'),
        ]
        self.root = root
        self.lock = threading.Lock()
        self.cert, key = root / 'certificate.pem', root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(self.cert), '-days', '1',
                        '-subj', '/CN=*.googleapis.com', '-addext',
                        'subjectAltName=DNS:*.googleapis.com'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, key)
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_CONNECT(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                try:
                    self.connection = context.wrap_socket(self.connection, server_side=True)
                    self.connection.settimeout(30)
                    self.rfile = self.connection.makefile('rb')
                    self.wfile = self.connection.makefile('wb')
                    self.close_connection = False
                    while not self.close_connection:
                        self.handle_one_request()
                except (ssl.SSLError, OSError):
                    self.close_connection = True

            def do_GET(self):
                self.reply({}, 'application/json')

            def reply(self, data, content_type):
                raw = (('data: ' + json.dumps(data) + '\n\n') if
                       content_type == 'text/event-stream' else json.dumps(data)).encode()
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()

            def do_POST(self):
                if self.headers.get('Transfer-Encoding') == 'chunked':
                    chunks = []
                    while True:
                        size = int(self.rfile.readline().split(b';')[0], 16)
                        if not size:
                            self.rfile.readline()
                            break
                        chunks.append(self.rfile.read(size))
                        self.rfile.read(2)
                    raw = b''.join(chunks)
                else:
                    raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                try:
                    body = json.loads(raw or b'{}')
                except (UnicodeDecodeError, ValueError):
                    # Telemetry uses protobuf. It is unrelated to model responses.
                    self.reply({}, 'application/json')
                    return
                if 'GenerateContent' in self.path or 'generateContent' in self.path:
                    part = {'text': 'Offline title'}
                    if body.get('requestType') == 'agent':
                        with fixture.lock:
                            index = len(fixture.requests)
                            fixture.requests.append(body)
                            (root / 'requests.json').write_text(json.dumps(fixture.requests))
                            part = fixture.responses[index] if index < len(fixture.responses) else {
                                'text': 'UNEXPECTED_EXTRA_REQUEST'}
                    self.reply({'response': {'candidates': [{'index': 0,
                        'content': {'role': 'model', 'parts': [part]}, 'finishReason': 'STOP'}],
                        'usageMetadata': {'promptTokenCount': 20, 'candidatesTokenCount': 5,
                                          'totalTokenCount': 25}, 'modelVersion': MODEL}},
                               'text/event-stream')
                elif 'loadCodeAssist' in self.path:
                    self.reply({'cloudaicompanionProject': 'offline-fixture',
                                'currentTier': {'id': 'pro-tier', 'name': 'Pro'}}, 'application/json')
                elif 'fetchAvailableModels' in self.path:
                    self.reply({'models': {MODEL: {'displayName': 'Gemini Flash Low',
                        'supportsImages': True, 'maxTokens': 1048576, 'maxOutputTokens': 8192,
                        'model': 312, 'apiProvider': 24}}, 'agentModelSorts': [
                        {'displayName': 'Available', 'groups': [{'displayName': 'Gemini',
                         'modelIds': [MODEL]}]}], 'defaultAgentModelId': MODEL}, 'application/json')
                else:
                    self.reply({}, 'application/json')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def decode(response, stream):
    assert response.status_code == 200, response.text
    assert 'UNSUBMITTED_PRIVATE_' not in response.text, response.text
    if not stream:
        choice = response.json()['choices'][0]
        return choice['message'], choice['finish_reason']
    assert response.text.count('data: [DONE]') == 1, response.text
    message = {'role': 'assistant', 'content': ''}
    finished = []
    for line in response.text.splitlines():
        if not line.startswith('data: ') or line == 'data: [DONE]':
            continue
        event = json.loads(line[6:])
        assert not event.get('error'), event
        for choice in event.get('choices', []):
            delta = choice['delta']
            message['content'] += delta.get('content') or ''
            if delta.get('tool_calls'):
                assert 'tool_calls' not in message, 'Duplicate tool delivery'
                message['tool_calls'] = [{k: v for k, v in call.items() if k != 'index'}
                                         for call in delta['tool_calls']]
            if choice.get('finish_reason'):
                finished.append(choice['finish_reason'])
    assert len(finished) == 1, finished
    return message, finished[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    symbols = root / 'symbols.json'
    subprocess.run([sys.executable, str(REPO / 'tools/agy_native_wire_probe/symbols.py')],
                   env=dict(os.environ, AGY_WIRE_BINARY=str(args.binary.resolve()),
                            AGY_WIRE_SYMBOLS=str(symbols)), check=True, stdout=subprocess.DEVNULL)
    state = root / 'home/.gemini/antigravity-cli'
    state.mkdir(parents=True)
    (state / 'antigravity-oauth-token').write_text(json.dumps({'token': {
        'access_token': 'OFFLINE_FIXTURE_ONLY', 'refresh_token': 'OFFLINE_FIXTURE_ONLY',
        'token_type': 'Bearer', 'expiry': '2099-01-01T00:00:00Z'}, 'auth_method': 'consumer'}))
    fixture = Upstream(root)
    proxy = fixture.start()
    os.environ['SSL_CERT_FILE'] = str(fixture.cert)
    os.environ.pop('CLOUD_CODE_URL', None)
    # Keep the real CLI endpoint and transport; only the isolated proxy returns fixtures.
    from fastapi.testclient import TestClient
    from app import create_app
    from app.config import Config
    cfg = Config(agy_bin=str(args.binary.resolve()), native_bin=str(args.binary.resolve()),
                 native_chat=True, native_input_clean=True, native_symbols=str(symbols),
                 data_dir=str(root / 'data'), account_home=str(root / 'home'), http_proxy=proxy,
                 turn_timeout=45, stream_heartbeat=.1, password='offline-fixture',
                 api_keys=('offline-fixture',))
    logging.getLogger('agy_proxy.native_chat').setLevel(logging.INFO)
    messages = [{'role': 'user', 'content': 'Read client-one.txt with the client tool.'}]
    expected_requests = [1, 3, 5, 7, 9]
    summaries, identities, tool_ids = [], [], []
    try:
        with TestClient(create_app(cfg)) as client:
            for index in range(5):
                stream = index % 2 == 1
                response = client.post('/v1/chat/completions', json={
                    'model': MODEL, 'tools': [TOOL], 'messages': messages, 'stream': stream},
                    headers={'X-Agy-Session-Id': 'offline-finish-client',
                             'Authorization': 'Bearer offline-fixture'})
                message, reason = decode(response, stream)
                record = list(client.app.state.store.requests)[-1].public()
                assert record['attempts'] == 1 and record['repair_attempts'] == 0, record
                assert record['usage_complete'], record
                expected_input = 20 if index == 0 else 40
                assert record['usage']['input_tokens'] == expected_input, record
                assert len(fixture.requests) == expected_requests[index], len(fixture.requests)
                sessions = client.app.state.native_sessions
                assert sessions.live_count == 1 and sessions.busy_count() == 0
                session, = [s for s in sessions._sessions if s.alive]
                identities.append((session.observer.inferior_pid, session.conversation_id))
                assert session.observer.last_metrics['cli_turns'] == expected_requests[index]
                messages.append(message)
                if index in (0, 2):
                    assert reason == 'tool_calls', message
                    call, = message['tool_calls']
                    assert call['function']['name'] == 'read_fixture'
                    path = 'client-one.txt' if index == 0 else 'client-two.txt'
                    assert json.loads(call['function']['arguments']) == {'path': path}
                    tool_ids.append(call['id'])
                    messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                     'content': 'CLIENT_RESULT_ONE' if index == 0 else 'CLIENT_RESULT_TWO'})
                else:
                    assert reason == 'stop'
                    assert message['content'] == {1: 'CLIENT_RESULT_ONE', 3: 'CLIENT_RESULT_TWO',
                                                   4: 'FINAL_CURRENT_TURN'}[index]
                    if index < 4:
                        messages.append({'role': 'user', 'content':
                            'Read client-two.txt.' if index == 1 else 'Give the final answer.'})
                summaries.append({'turn': index + 1, 'stream': stream, 'finish_reason': reason,
                                  'attempts': record['attempts'], 'repair_attempts': record['repair_attempts'],
                                  'usage': record['usage'], 'agent_requests': len(fixture.requests)})
                print(json.dumps(summaries[-1]), flush=True)
            assert len(set(identities)) == 1, identities
            assert len(set(tool_ids)) == 2
            for request in fixture.requests:
                assert request['request']['toolConfig']['functionCallingConfig'] == {
                    'mode': 'ANY', 'allowedFunctionNames': ['finish']}
            (root / 'summary.json').write_text(json.dumps(summaries, indent=2))
    finally:
        fixture.close()
    print('PASS: real CLI, TLS capture, 5 client turns, 4 internal recoveries, one session, no replay')


if __name__ == '__main__':
    main()
