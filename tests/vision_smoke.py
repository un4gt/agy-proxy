"""Live image -> coding -> resumed coding check for four isolated CLI clients.

Run: uv run python tests/vision_smoke.py
The expected pixels are checked by a verifier outside the agent's workspace.
Uses .test.env, no user-wide config changes; captures stay in ignored .preview.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import secrets as random_secrets
import subprocess
import threading
import time
import uuid

from PIL import Image, ImageDraw, ImageFont

import mcp_smoke as common


CONTRACT = """import test from 'node:test';
import assert from 'node:assert/strict';
import {getBoard, renderBoard} from './board.mjs';
test('board has code and three shapes',()=>{const b=getBoard();assert.match(b.code,/^[A-Z0-9]{4}$/);assert.equal(b.shapes.length,3)});
test('shape vocabulary',()=>{for(const s of getBoard().shapes){assert.ok(['red','green','blue'].includes(s.color));assert.ok(['circle','square','triangle'].includes(s.shape))}});
test('renders code and shape metadata',()=>{const b=getBoard(),h=renderBoard();assert.ok(h.includes(b.code));for(const s of b.shapes){assert.ok(h.includes(`data-shape="${s.shape}"`));assert.ok(h.includes(`data-color="${s.color}"`))}});
test('fresh data',()=>{const b=getBoard(),before=JSON.stringify(b);b.shapes.reverse();b.code='XXXX';assert.equal(JSON.stringify(getBoard()),before)});
"""
MORE = """
test('reverse is opt-in',()=>{assert.deepEqual(getBoard({reverse:true}).shapes,getBoard().shapes.toReversed());assert.deepEqual(getBoard({reverse:false}),getBoard())});
test('reverse preserves code and default',()=>{const a=getBoard();assert.equal(getBoard({reverse:true}).code,a.code);assert.deepEqual(getBoard(),a)});
test('reverse render order',()=>{const b=getBoard({reverse:true}),h=renderBoard({reverse:true});let p=-1;for(const s of b.shapes){const n=h.indexOf(`data-shape="${s.shape}"`);assert.ok(n>p);p=n}});
"""


def picture(path, index):
    colors = ['green', 'red', 'blue']
    shapes = ['circle', 'square', 'triangle']
    pairs = [{'color': colors[(i + index) % 3], 'shape': shapes[(i + index // 2) % 3]} for i in range(3)]
    code = ''.join(random_secrets.choice('ABCDEFGHJKLMNPRSTUVWXYZ23456789') for _ in range(4))
    image = Image.new('RGB', (720, 420), 'white')
    draw = ImageDraw.Draw(image)
    for i, item in enumerate(pairs):
        x = 30 + i * 240
        fill = {'green':'#008000','red':'#e00000','blue':'#0000e0'}[item['color']]
        if item['shape'] == 'circle':
            draw.ellipse((x, 40, x + 170, 210), fill=fill)
        elif item['shape'] == 'square':
            draw.rectangle((x, 40, x + 170, 210), fill=fill)
        else:
            draw.polygon([(x+85, 40), (x, 210), (x+170, 210)], fill=fill)
    font = ImageFont.truetype('C:/Windows/Fonts/consolab.ttf', 76)
    draw.text((250, 290), code, font=font, fill='black')
    image.save(path)
    return {'code': code, 'shapes': pairs}


def configure(name, folder, origin, key):
    directory, work, env, command = common.setup_client(name, folder, origin, key)
    for filename in ('notes.mjs', 'notes.test.mjs', 'requirements.md'):
        (work / filename).unlink()
    (work / 'board.mjs').write_text('export function getBoard() { return {code:"",shapes:[]}; }\nexport function renderBoard() { return ""; }\n', encoding='utf-8')
    (work / 'board.test.mjs').write_text(CONTRACT, encoding='utf-8')
    cfg = directory / 'config'
    if name == 'pi':
        model_path = cfg / 'models.json'
        models = json.loads(model_path.read_text(encoding='utf-8'))
        models['providers']['agy']['models'][0]['input'] = ['text', 'image']
        common.write_json(model_path, models)
        mcp = json.loads((cfg / 'mcp.json').read_text(encoding='utf-8'))
        mcp['mcpServers'].pop('exa')
        common.write_json(cfg / 'mcp.json', mcp)
    elif name == 'codex':
        catalog = common.ROOT / 'examples/vision/codex-models.json'
        command += ['-c', 'model_catalog_json=' + common.toml_value(str(catalog))]
        # Retain the proven filesystem MCP and Windows sandbox configuration.
        for i, part in enumerate(command):
            if part.startswith('mcp_servers='):
                fs = {'filesystem': {'command': str(common.NPX), 'args': ['-y', '@modelcontextprotocol/server-filesystem', str(work)],
                      'startup_timeout_sec': 30, 'tools': {t: {'approval_mode': 'approve'} for t in ('read_text_file','read_multiple_files','write_file','edit_file')}}}
                command[i] = 'mcp_servers=' + common.toml_value(fs)
    elif name == 'claude':
        for k in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
                  'ANTHROPIC_DEFAULT_OPUS_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL'):
            env[k] = common.MODEL
        command[command.index('--model') + 1] = common.MODEL
        # Keep scoped tool allowlists without auto-mode's unrelated, text-only
        # safety-classifier calls and its third-party billing warning.
        command += ['--input-format', 'stream-json', '--permission-mode', 'default']
        mcp = json.loads((cfg / 'mcp.json').read_text(encoding='utf-8'))
        mcp['mcpServers'].pop('exa')
        common.write_json(cfg / 'mcp.json', mcp)
    else:
        conf = json.loads((cfg / 'opencode.json').read_text(encoding='utf-8'))
        conf['mcp'].pop('exa')
        conf['provider']['agy']['models'][common.MODEL].update(attachment=True, modalities={'input':['text','image'],'output':['text']})
        common.write_json(cfg / 'opencode.json', conf)
    return directory, work, env, command


def run(command, work, env, log, secrets, stdin=None):
    with log.open('w', encoding='utf-8') as output:
        process = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        def drain():
            for line in process.stdout:
                output.write(common.redact(line, secrets))
                output.flush()
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        if stdin:
            process.stdin.write(stdin)
            process.stdin.close()
        try:
            code = process.wait(timeout=900)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
            process.wait()
            code = 124
        reader.join(timeout=10)
    return code


def verify(work, expected, phase, log):
    script = "import assert from 'node:assert/strict';import {getBoard,renderBoard} from './board.mjs';\n"
    script += 'const expected=' + json.dumps(expected) + ';\n'
    script += "assert.deepEqual(getBoard(),expected);assert.deepEqual(getBoard({reverse:false}),expected);assert.ok(renderBoard().includes(expected.code));\n"
    if phase == 2:
        script += "assert.deepEqual(getBoard({reverse:true}),{...expected,shapes:expected.shapes.toReversed()});\n"
    script += "console.log('Pixel ground truth verified');\n"
    result = subprocess.run(['node','--input-type=module'],input=script,cwd=work,capture_output=True,text=True,encoding='utf-8')
    log.write_text(result.stdout + result.stderr, encoding='utf-8')
    return result.returncode == 0


def image_blocks(value):
    if isinstance(value, dict):
        if value.get('type') in ('image_url', 'input_image', 'image'):
            return 1
        return sum(image_blocks(v) for v in value.values())
    if isinstance(value, list):
        return sum(image_blocks(v) for v in value)
    return 0


def warnings_in(log):
    warnings = []
    for line in log.read_text(encoding='utf-8').splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            if re.search(r'\bWARN(?:ING)?\b|Warning:', line, re.I):
                warnings.append(line[:300])
            continue
        if value.get('type') == 'warning' or value.get('level') in ('warn', 'warning'):
            warnings.append(value)
        if value.get('type') == 'item.completed' and value.get('item', {}).get('type') == 'error':
            warnings.append(value['item'])
    return warnings


def audit_images(folder, records, clients):
    summary = {}
    for name in clients:
        counts = []
        for record in records:
            if record['client'] == name:
                request = folder / name / 'wire' / f"{record['index']:03}-request.json"
                counts.append(image_blocks(json.loads(request.read_text(encoding='utf-8'))))
        # These clients replay full history. Every tool iteration and resumed
        # turn must retain the original attachment, not merely its path text.
        summary[name] = {'image_blocks_per_request': counts,
                         'all_requests_keep_image': bool(counts) and all(n == 1 for n in counts)}
    common.write_json(folder / 'images-summary.json', summary)
    return summary


def run_client(name, folder, origin, key, secrets):
    directory, work, env, command = configure(name, folder, origin, key)
    expected = picture(directory / 'reference.png', list(common.PROTOCOLS).index(name))
    marker = name.upper() + '_' + uuid.uuid4().hex[:12]
    test_cmd = 'node --test --test-isolation=none' if name == 'codex' else 'node --test'
    prompt = (f'Work only in {work}. Remember session marker {marker}. '
              'Use the attached image as the only source for the exact four-character code and the three colored shapes in left-to-right order. '
              'Implement board.mjs: getBoard() returns {code,shapes:[{color,shape}]} with lowercase English color and shape names; '
              'return fresh objects each time. renderBoard() returns HTML showing the code and shapes in order, '
              'each shape element must have double-quoted data-shape and data-color attributes. '
              'Use filesystem MCP tools to read board.mjs and board.test.mjs, and write board.mjs. '
              'Do not use built-in file tools or shell for file I/O. Do not change tests. '
              f'Run `{test_cmd}` with your shell tool. Fix until tests pass. '
              'Finish with a concise result, the image code, and the session marker. Do the work now.')
    phases = []
    for phase in (1, 2):
        args = list(command)
        if phase == 1:
            if name == 'pi':
                args += ['@' + str(directory / 'reference.png')]
            elif name == 'codex':
                args += ['--image', str(directory / 'reference.png')]
            elif name == 'opencode':
                args += ['--file', str(directory / 'reference.png')]
        else:
            (work / 'board.test.mjs').write_text(CONTRACT + MORE, encoding='utf-8')
            prompt = ('Continue this same session using the prior attached image. Add getBoard({reverse:true}) and renderBoard({reverse:true}) '
                      'that reverse only shape order, preserve the original code and keep default behavior. No new image is attached. '
                      f'Use your available client tools to modify the implementation, do not modify tests, run `{test_cmd}` and fix failures. '
                      'Finish with the original image code and the session marker I gave you in the first turn, without searching files for the marker.')
            sid = common.session_id(name, directory / 'phase1.jsonl')
            if name == 'codex':
                args += ['resume', sid]
            elif name == 'claude':
                args += ['--resume', sid]
            elif name == 'opencode':
                args += ['--session', sid]
        stdin = None
        if name == 'claude':
            content = [{'type':'text','text':prompt}]
            if phase == 1:
                content.insert(0, {'type':'image','source':{'type':'base64','media_type':'image/png',
                    'data':base64.b64encode((directory / 'reference.png').read_bytes()).decode()}})
            stdin = json.dumps({'type':'user','message':{'role':'user','content':content}}) + '\n'
        else:
            # Stop variadic --file/--image from consuming the positional prompt.
            if name in ('opencode', 'codex'):
                args += ['--']
            args += [prompt]
        print(f'Starting {name} image phase {phase}', flush=True)
        log = directory / f'phase{phase}.jsonl'
        exit_code = run(args, work, env, log, secrets, stdin)
        contract = CONTRACT + (MORE if phase == 2 else '')
        unchanged = (work / 'board.test.mjs').read_text(encoding='utf-8') == contract
        (work / 'board.test.mjs').write_text(contract, encoding='utf-8')
        test = subprocess.run(['node','--test','board.test.mjs'],cwd=work,capture_output=True,text=True,encoding='utf-8')
        (directory / f'phase{phase}-tests.log').write_text(test.stdout + test.stderr, encoding='utf-8')
        audit = common.audit_log(name, log, marker, phase)
        # This check uses filesystem MCP only; Exa is covered by mcp_smoke.py.
        audit.pop('exa_search', None)
        audit.pop('mcp_pass', None)
        result = {'phase':phase,'exit_code':exit_code,'tests_pass':test.returncode == 0,'tests_unchanged':unchanged,
                  'pixels_pass':verify(work, expected, phase, directory / f'phase{phase}-pixels.log'),
                  'client_warnings': warnings_in(log), **audit}
        result['passed'] = (exit_code == 0 and result['tests_pass'] and unchanged and result['pixels_pass'] and
                            (phase == 2 or audit['filesystem_write']) and audit['client_ran_tests'] and audit['session_marker_present'] and
                            audit['last_shell_pass'] and not audit['tool_errors'] and not result['client_warnings'])
        phases.append(result)
        common.write_json(directory / 'result.json', phases)
        print(f'Finished {name} phase {phase}: {json.dumps(result)}', flush=True)
        if not result['passed']:
            break
    return phases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clients', nargs='+', choices=list(common.PROTOCOLS), default=list(common.PROTOCOLS))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--parallel', action='store_true', help='Run isolated clients concurrently (requires enough proxy slots)')
    args = parser.parse_args()
    # Vision needs the proxy key only, not an Exa account.
    values = {}
    for line in (common.ROOT / '.test.env').read_text(encoding='utf-8-sig').splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            values[k.strip()] = v.strip().strip('\"\'')
    base, key = values['BASE_URL'].rstrip('/').removesuffix('/v1'), values['API_KEY']
    secrets = [key, os.environ.get('EXA_API_KEY', '')]
    folder = (args.output or common.ROOT / '.preview' / time.strftime('vision-clients-%Y%m%d-%H%M%S')).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    print(f'Artifacts: {folder}', flush=True)
    origin, server, thread, records = common.relay(folder, base, key, secrets)
    results = {}
    try:
        if args.parallel:
            with ThreadPoolExecutor(max_workers=len(args.clients)) as pool:
                pending = {pool.submit(run_client, name, folder, origin, key, secrets): name for name in args.clients}
                for future in as_completed(pending):
                    results[pending[future]] = future.result()
                    common.write_json(folder / 'results.json', results)
        else:
            for name in args.clients:
                results[name] = run_client(name, folder, origin, key, secrets)
                common.write_json(folder / 'results.json', results)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        common.write_json(folder / 'results.json', results)
        common.write_json(folder / 'requests.json', records)
    wire = common.audit_wire(folder, records, args.clients)
    images = audit_images(folder, records, args.clients)
    passed = all(r['protocol_pass'] and not r['http_errors'] and not r['stream_errors'] for r in wire.values())
    passed = passed and all(r['all_requests_keep_image'] for r in images.values())
    passed = passed and all(len(rows) == 2 and all(r['passed'] for r in rows) for rows in results.values())
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
