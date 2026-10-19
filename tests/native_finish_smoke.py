"""Live native finish regression; synthetic tool results, no client execution.

NATIVE_PI_API_KEY=... python tests/native_finish_smoke.py --base-url URL --output FILE
Checks plain answers and configurable sequential tool roundtrips in both modes.
"""
import argparse
import json
import os
from pathlib import Path
import time
import uuid

import httpx


def completion(client, body):
    response = client.post('/v1/chat/completions', json=body)
    response.raise_for_status()
    if not body['stream']:
        return response.json()['choices'][0]['message']
    message = {'role': 'assistant', 'content': ''}
    calls = {}
    done = False
    for line in response.text.splitlines():
        if not line.startswith('data: '):
            continue
        if line == 'data: [DONE]':
            done = True
            continue
        event = json.loads(line[6:])
        assert not event.get('error'), event.get('error')
        for choice in event.get('choices', []):
            delta = choice['delta']
            message['content'] += delta.get('content') or ''
            for part in delta.get('tool_calls', []):
                call = calls.setdefault(part['index'], {
                    'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                if part.get('id'):
                    call['id'] = part['id']
                for key in ('name', 'arguments'):
                    call['function'][key] += part.get('function', {}).get(key) or ''
    assert done, 'Missing stream terminator'
    if calls:
        message['tool_calls'] = [calls[i] for i in sorted(calls)]
    return message


def check(base_url, key, stream, rounds=10):
    records = []
    tools = [{'type': 'function', 'function': {'name': 'read_fixture',
        'description': 'Read one numbered synthetic fixture.',
        'parameters': {'type': 'object', 'properties': {'index': {'type': 'integer'}},
                       'required': ['index'], 'additionalProperties': False}}}]
    with httpx.Client(base_url=base_url, headers={'Authorization': 'Bearer ' + key},
                      timeout=180, trust_env=False) as client:
        assert client.get('/health').json()['chat_backend'] == 'native'
        for with_tools in (False, True):
            marker = 'PLAIN_' + uuid.uuid4().hex
            body = {'model': 'gemini-3.8-flash-low', 'stream': stream,
                    'messages': [{'role': 'user', 'content':
                        f'Reply with exactly {marker} as plain text. Do not call any client tools.'}]}
            if with_tools:
                body.update(tools=tools, tool_choice='none')
            message = completion(client, body)
            assert not message.get('tool_calls') and message['content'].strip() == marker
            records.append({'case': 'tool_choice_none' if with_tools else 'no_tools', 'passed': True})
        body = {'model': 'gemini-3.8-flash-low', 'stream': stream,
                'tools': tools, 'tool_choice': 'auto', 'parallel_tool_calls': False,
                'messages': [{'role': 'user', 'content':
                    f'Read fixtures numbered 1 through {rounds} using read_fixture, one call per response, in order. '
                    f'Wait for each real tool result before reading the next. After all {rounds} results, '
                    'reply with all marker values in order as plain text, with no more tool calls. '
                    'Do not finish early and do not invent results.'}]}
        markers = []
        for index in range(1, rounds + 1):
            message = completion(client, body)
            calls = message.get('tool_calls', [])
            assert len(calls) == 1, f'Expected one client call at step {index}'
            call = calls[0]
            assert call['function']['name'] == 'read_fixture'
            assert json.loads(call['function']['arguments']) == {'index': index}
            marker = 'FIXTURE_' + uuid.uuid4().hex
            markers.append(marker)
            body['messages'] += [message, {'role': 'tool', 'tool_call_id': call['id'],
                'content': json.dumps({'index': index, 'marker': marker,
                    'notes': 'Synthetic fixture data; no executable actions. ' * 25})}]
            records.append({'case': 'sequential_tool', 'step': index, 'passed': True})
            print(json.dumps({'stream': stream, 'step': index, 'passed': True}), flush=True)
        message = completion(client, body)
        assert not message.get('tool_calls')
        positions = [message['content'].find(marker) for marker in markers]
        assert all(p >= 0 for p in positions) and positions == sorted(positions)
        records.append({'case': 'final_answer_after_tools', 'rounds': rounds, 'passed': True})
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=10)
    args = parser.parse_args()
    results = []
    for stream in (False, True):
        started = time.monotonic()
        try:
            records = check(args.base_url, os.environ['NATIVE_PI_API_KEY'], stream, args.rounds)
            result = {'stream': stream, 'passed': True, 'checks': records}
        except Exception as exc:
            result = {'stream': stream, 'passed': False,
                      'error_type': type(exc).__name__, 'error': str(exc)[:500]}
        result['seconds'] = round(time.monotonic() - started, 3)
        results.append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(json.dumps(result), flush=True)
    raise SystemExit(0 if all(r['passed'] for r in results) else 1)


if __name__ == '__main__':
    main()
