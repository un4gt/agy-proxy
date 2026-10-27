"""Live native API image checks using synthetic pixels and isolated account state.

Run inside a disposable Linux container with GDB and the pinned CLI. The output
directory and account-home must be private; never point account-home at live state.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import time

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from app import create_app
from app.config import Config
import app.native_chat as native_chat


def fixtures(root):
    answers, urls = [], []
    layouts = [('red triangle', 'blue circle', 'green square'),
               ('green circle', 'red square', 'blue triangle')]
    for index, shapes in enumerate(layouts):
        code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
        image = Image.new('RGB', (720, 360), 'white')
        draw = ImageDraw.Draw(image)
        draw.text((100, 30), code, fill='black', font=ImageFont.load_default(size=54))
        for slot, item in enumerate(shapes):
            color, shape = item.split()
            left = 50 + slot * 230
            box = (left, 160, left + 150, 310)
            if shape == 'circle':
                draw.ellipse(box, fill=color)
            elif shape == 'square':
                draw.rectangle(box, fill=color)
            else:
                draw.polygon([(left + 75, 160), (left, 310), (left + 150, 310)], fill=color)
        file = root / f'image-{index + 1}.png'
        image.save(file)
        urls.append('data:image/png;base64,' + base64.b64encode(file.read_bytes()).decode())
        answers.append(dict(code=code, **dict(zip(('left', 'center', 'right'), shapes))))
    return answers, urls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--account-home', required=True)
    parser.add_argument('--binary', default='/opt/agy-native/agy')
    parser.add_argument('--symbols', default='/opt/agy-native/symbols.json')
    parser.add_argument('--model', default='gemini-3.8-flash-low')
    parser.add_argument('--cases', nargs='+', default=['single', 'multiple_stream', 'same_session_followup', 'pi_tool_image'],
                        choices=['single', 'multiple_stream', 'same_session_followup', 'pi_tool_image'])
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    answers, urls = fixtures(args.output)
    key = secrets.token_hex(16)
    config = Config(agy_bin=args.binary, native_bin=args.binary, native_symbols=args.symbols,
                    account_home=args.account_home, data_dir=str(args.output / 'data'),
                    native_chat=True, native_input_clean=True, prewarm_models=(),
                    api_keys=(key,), password=secrets.token_hex(16), turn_timeout=180)
    schema = {'type': 'object', 'properties': {'images': {'type': 'array', 'items': {
        'type': 'object', 'properties': {k: {'type': 'string'} for k in ('code', 'left', 'center', 'right')},
        'required': ['code', 'left', 'center', 'right'], 'additionalProperties': False}}},
        'required': ['images'], 'additionalProperties': False}
    prompt = ('Inspect every attached image. Return ONLY a JSON object with an images array in image order. '
              'For each image copy the eight-character code exactly and identify the left, center, '
              'and right shapes as lowercase color and shape, such as yellow star. '
              'Read the pixels; do not guess.')
    observed = []
    original = native_chat.captured_output

    def audit(result, **kwargs):
        observed.append({'calls': [c.get('name') for c in result['calls']],
                         'images_seen': len(kwargs['images_seen']), **result['metrics']})
        return original(result, **kwargs)

    native_chat.captured_output = audit
    records = []
    try:
        with TestClient(create_app(config)) as client:
            assert client.get('/health').json()['chat_backend'] == 'native'
            history = None
            for name, indices, stream in [('single', [0], False), ('multiple_stream', [0, 1], True),
                                           ('same_session_followup', [0, 1], False),
                                           ('pi_tool_image', [1], True)]:
                if name not in args.cases:
                    continue
                if name != 'same_session_followup':
                    content = [{'type': 'text', 'text': prompt}] + [
                        {'type': 'image_url', 'image_url': {'url': urls[i]}} for i in indices]
                    history = [{'role': 'user', 'content': content}]
                else:
                    assert history is not None and 'multiple_stream' in args.cases
                    history.append({'role': 'user', 'content': 'Inspect the original images again. ' + prompt})
                if name == 'pi_tool_image':
                    history = [{'role': 'user', 'content': 'Read the image attachment returned by the client.'},
                        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'fixture-read',
                         'type': 'function', 'function': {'name': 'read', 'arguments': '{"path":"fixture.png"}'}}]},
                        {'role': 'tool', 'tool_call_id': 'fixture-read', 'content': 'Read image file [image/png]'},
                        *history]
                started = time.monotonic()
                before = len(observed)
                body = {'model': args.model, 'stream': stream, 'messages': history}
                if name != 'pi_tool_image':
                    body['response_format'] = {'type': 'json_schema', 'json_schema': {
                        'name': 'image_inspection', 'strict': True, 'schema': schema}}
                response = client.post('/v1/chat/completions', json=body,
                    headers={'Authorization': 'Bearer ' + key})
                record = {'case': name, 'status': response.status_code,
                          'seconds': round(time.monotonic() - started, 3), 'capture': observed[before:]}
                records.append(record)
                try:
                    assert response.status_code == 200, response.text[:500]
                    if stream:
                        text = ''
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
                                text += choice.get('delta', {}).get('content') or ''
                        assert done
                    else:
                        text = response.json()['choices'][0]['message']['content']
                    actual = json.loads(text)
                    assert actual == {'images': [answers[i] for i in indices]}, actual
                    record.update(passed=True, answer=actual)
                    history.append({'role': 'assistant', 'content': text})
                    request = list(client.app.state.store.requests)[-1].public()
                    record.update(usage=request['usage'], usage_complete=request['usage_complete'],
                                  attempts=request['attempts'], repair_attempts=request['repair_attempts'])
                    assert record['usage_complete'] and record['repair_attempts'] == 0
                except Exception as exc:
                    record.update(passed=False, error=str(exc)[:1000])
                    raise
                finally:
                    (args.output / 'report.json').write_text(json.dumps(records, indent=2))
                    print(json.dumps(record), flush=True)
    finally:
        native_chat.captured_output = original


if __name__ == '__main__':
    main()
