"""Deterministic Linux CLI/GDB regression; use a network-disabled container.

No real account or upstream is used. The CLI still executes its real planner,
finish converter, force-invocation hooks and stream-json result builder.

python tests/native_finish_boundary_smoke.py --binary /opt/agy-native/agy --output /evidence
"""
import argparse
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


REPO = Path(__file__).resolve().parents[1]
MODEL = 'gemini-3.8-flash-low'
SCHEMA = {'type': 'object', 'properties': {'value': {'type': 'string'}},
          'required': ['value'], 'additionalProperties': False}
GDB_SCRIPT = '''
import gdb, json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ['FINISH_PROBE_MODULES'])
from finish_guard import install
for command in ['set pagination off', 'set confirm off', 'set print thread-events off',
                'set debuginfod enabled off', 'handle SIGURG nostop noprint pass',
                'handle SIGPIPE nostop noprint pass']:
    gdb.execute(command)
gdb.execute('starti')
inferior = gdb.selected_inferior()
row = next(row for row in Path('/proc/' + str(inferior.pid) + '/maps').read_text().splitlines()
           if row.endswith(' ' + os.environ['FINISH_BINARY']) and row.split()[2] == '00000000')
base = int(row.split('-')[0], 16)
events = Path(os.environ['FINISH_EVENTS']).open('w')
def emit(kind, **fields):
    events.write(json.dumps(dict(kind=kind, **fields)) + '\\n'); events.flush()
hooks = install(gdb, base=base, symbols=json.loads(Path(os.environ['FINISH_SYMBOLS']).read_text()),
                emit=emit, kill=os.kill)
gdb.execute('continue')
'''


def run_case(binary, output, symbols, *, name, guarded, missing, turns, full_probe=False):
    root = output / name
    root.mkdir()
    home, work = root / 'home', root / 'workspace'
    state = home / '.gemini/antigravity-cli'
    state.mkdir(parents=True)
    (state / 'antigravity-oauth-token').write_text(json.dumps({
        'token': {'access_token': 'OFFLINE_FIXTURE_ONLY', 'refresh_token': 'OFFLINE_FIXTURE_ONLY',
                  'token_type': 'Bearer', 'expiry': '2099-01-01T00:00:00Z'}, 'auth_method': 'consumer'}))
    agents = work / '.agents/agents'
    agents.mkdir(parents=True)
    (agents / 'proxy-external.md').write_text(
        '---\nname: proxy-external\ndescription: Isolated finish regression\n'
        'tools: [finish]\nmainAgent: true\nsubagent: false\ncommandExecutionPolicy: off\n'
        '---\nYou are the reasoning backend for an external coding client.\n'
        'Use finish exactly once to submit the response.\n')
    (work / 'schema.json').write_text(json.dumps(SCHEMA))
    requests = []
    agent_requests = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

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
            body = json.loads(raw or b'{}')
            with lock:
                requests.append({'path': self.path, 'body': body})
                if body.get('requestType') == 'agent':
                    agent_requests.append(body)
                number = len(agent_requests)
            if 'GenerateContent' in self.path or 'generateContent' in self.path:
                if body.get('requestType') != 'agent':
                    parts = [{'text': 'Fixture title'}]
                elif number in missing:
                    parts = [{'text': 'MISSING_FINISH_' + str(number)}]
                else:
                    parts = [{'functionCall': {'name': 'finish', 'args': {
                        'value': 'FIXTURE_' + str(number), 'toolAction': 'respond', 'toolSummary': 'Fixture'}}}]
                data = {'response': {'candidates': [{'index': 0, 'content': {'role': 'model', 'parts': parts},
                                                     'finishReason': 'STOP'}],
                                     'usageMetadata': {'promptTokenCount': 20, 'candidatesTokenCount': 5,
                                                       'totalTokenCount': 25}, 'modelVersion': MODEL}}
                payload, content_type = ('data: ' + json.dumps(data) + '\n\n').encode(), 'text/event-stream'
            else:
                data = {}
                if 'loadCodeAssist' in self.path:
                    data = {'cloudaicompanionProject': 'offline-fixture',
                            'currentTier': {'id': 'pro-tier', 'name': 'Pro'}}
                elif 'fetchAvailableModels' in self.path:
                    data = {'models': {MODEL: {'displayName': 'Gemini Flash Low', 'supportsImages': True,
                                              'maxTokens': 1048576, 'maxOutputTokens': 8192,
                                              'model': 312, 'apiProvider': 24}},
                            'agentModelSorts': [{'displayName': 'Available', 'groups': [
                                {'displayName': 'Gemini', 'modelIds': [MODEL]}]}], 'defaultAgentModelId': MODEL}
                payload, content_type = json.dumps(data).encode(), 'application/json'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = dict(os.environ, HOME=str(home), SSH_CONNECTION='127.0.0.1 1 127.0.0.1 22',
               CLOUD_CODE_URL='http://127.0.0.1:' + str(server.server_port), NO_PROXY='127.0.0.1,localhost',
               FINISH_PROBE_MODULES=str(REPO / 'tools/agy_native_wire_probe'), FINISH_BINARY=str(binary),
               FINISH_SYMBOLS=str(symbols), FINISH_EVENTS=str(root / 'guard.jsonl'))
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(key, None)
    argv = [str(binary), '--agent', 'proxy-external', '--json-schema', str(work / 'schema.json'),
            '--model', MODEL, '--input-format', 'stream-json', '--output-format', 'stream-json',
            '--print-timeout', '20s']
    if guarded:
        script = root / 'guard_gdb.py'
        script.write_text(GDB_SCRIPT)
        if full_probe:
            script = REPO / 'tools/agy_native_wire_probe/probe_gdb.py'
            env.update(AGY_WIRE_SYMBOLS=str(symbols), AGY_WIRE_BINARY=str(binary),
                       AGY_WIRE_EVENTS=str(root / 'guard.jsonl'), AGY_WIRE_STDOUT='/dev/stdout',
                       AGY_WIRE_STDERR='/dev/stderr', AGY_WIRE_FINISH_GUARD='1', AGY_WIRE_INPUT_CLEAN='1')
        argv = ['gdb', '-q', '-batch', '-x', str(script), '--args'] + argv
    results = []
    lines = []
    received = queue.Queue()
    with (root / 'stderr.log').open('w') as stderr:
        proc = subprocess.Popen(argv, cwd=work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=stderr, text=True, start_new_session=True)

        def read():
            for line in proc.stdout:
                received.put(line)
            received.put(None)

        threading.Thread(target=read, daemon=True).start()
        try:
            for number in range(1, turns + 1):
                proc.stdin.write(json.dumps({'event': 'user', 'message': {
                    'content': 'Submit fixture turn ' + str(number) + ' using finish.'}}) + '\n')
                proc.stdin.flush()
                deadline = time.monotonic() + 30
                while True:
                    line = received.get(timeout=max(.01, deadline - time.monotonic()))
                    if line is None:
                        raise RuntimeError('CLI exited before result: ' + name)
                    lines.append(line)
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get('event') == 'result':
                        results.append(event['result'])
                        break
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            server.shutdown()
            server.server_close()
            (root / 'stdout.jsonl').write_text(''.join(lines))
            (root / 'results.json').write_text(json.dumps(results, indent=2))
            (root / 'requests.json').write_text(json.dumps(requests, indent=2))
    events = [json.loads(line) for line in (root / 'guard.jsonl').read_text().splitlines()] if guarded else []
    assert len({r['conversation_id'] for r in results}) == 1, name
    assert not any(e['kind'] == 'hook_error' for e in events), name
    if full_probe:
        for request in agent_requests:
            assert request['request']['toolConfig']['functionCallingConfig'] == {
                'mode': 'ANY', 'allowedFunctionNames': ['finish']}
    return {'case': name, 'agent_requests': len(agent_requests),
            'values': [r.get('structured_output', {}).get('value') for r in results],
            'cli_turns': [r['num_turns'] for r in results],
            'exhaustions': sum(e['kind'] == 'finish_exhausted' for e in events),
            'output_checks': [e['cli_turns'] for e in events if e['kind'] == 'finish_output_checked']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', help='Run only the named case')
    args = parser.parse_args()
    binary, output = args.binary.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    symbols = output / 'symbols.json'
    subprocess.run([sys.executable, str(REPO / 'tools/agy_native_wire_probe/symbols.py')], check=True,
                   env=dict(os.environ, AGY_WIRE_BINARY=str(binary), AGY_WIRE_SYMBOLS=str(symbols)),
                   stdout=subprocess.DEVNULL)
    cases = [
        dict(name='baseline', guarded=False, missing={2}, turns=2),
        dict(name='guarded', guarded=True, missing={2}, turns=2),
        dict(name='repeated', guarded=True, missing={2, 4, 6, 8}, turns=5),
        dict(name='exhausted', guarded=True, missing={2, 3, 4, 5, 6}, turns=2),
        dict(name='first_missing', guarded=True, missing={1}, turns=2),
        dict(name='all_finish', guarded=True, missing=set(), turns=2),
        dict(name='full_probe', guarded=True, missing={2}, turns=2, full_probe=True),
    ]
    expected = [(['FIXTURE_1', 'FIXTURE_1'], 2, 0),
                (['FIXTURE_1', 'FIXTURE_3'], 3, 0),
                (['FIXTURE_1', 'FIXTURE_3', 'FIXTURE_5', 'FIXTURE_7', 'FIXTURE_9'], 9, 0),
                (['FIXTURE_1', None], 5, 1),
                (['FIXTURE_2', 'FIXTURE_3'], 3, 0),
                (['FIXTURE_1', 'FIXTURE_2'], 2, 0),
                (['FIXTURE_1', 'FIXTURE_3'], 3, 0)]
    summary = []
    for case, (values, requests, exhaustions) in zip(cases, expected):
        if args.case and case['name'] != args.case:
            continue
        result = run_case(binary, output, symbols, **case)
        assert result['values'] == values, result
        assert result['agent_requests'] == requests, result
        assert result['exhaustions'] == exhaustions, result
        if case['guarded']:
            assert set(result['cli_turns']) <= set(result['output_checks']), result
        summary.append(result)
        print(json.dumps(result), flush=True)
        (output / 'summary.json').write_text(json.dumps(summary, indent=2))
    assert summary, 'No matching case'
    print(f'PASS: {len(summary)} real CLI cases, isolated fake upstream, no external network')


if __name__ == '__main__':
    main()
