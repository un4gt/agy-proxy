"""Real Pi -> local Chat Completions -> native CLI acceptance.

No user/global config is changed. Uses controlled fixtures and local MCP only.
uv run python tests/pi_native_smoke.py --scenario tools|mcp|skills
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import uuid

from skills_smoke import fixture as skill_fixture

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'gemini-3.8-flash-low'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scenario', choices=['tools', 'mcp', 'skills'], required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:18317/v1')
    parser.add_argument('--output', type=Path, default=ROOT / '.devdata/native-chat-pi-20261018/client')
    parser.add_argument('--pi-version', default='1.1.0')
    parser.add_argument('--pi-entry', type=Path, help='Installed pi CLI JS entry point; avoids npx startup/network work')
    parser.add_argument('--filesystem-server', type=Path, default=ROOT / '.preview/pi-taskbox-mcp-skills-20261014/deps/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js')
    args = parser.parse_args()
    directory = args.output.resolve() / (args.scenario + '-' + uuid.uuid4().hex[:8])
    work, config = directory / 'work', directory / 'config'
    work.mkdir(parents=True)
    config.mkdir()
    session_id = uuid.uuid4().hex
    env = dict(os.environ, PI_CODING_AGENT_DIR=str(config), PI_OFFLINE='1', PI_TELEMETRY='0',
               NATIVE_PI_API_KEY=os.environ.get('NATIVE_PI_API_KEY', 'native-pi-local-test'),
               NATIVE_PI_SESSION=session_id)
    save(config / 'models.json', {'providers': {'agy-native': {
        'baseUrl': args.base_url, 'api': 'openai-completions', 'apiKey': '${NATIVE_PI_API_KEY}', 'authHeader': True,
        'headers': {'X-Agy-Session-Id': session_id},
        'models': [{'id': MODEL, 'name': 'Local native AGY', 'reasoning': False,
                    'input': ['text'], 'contextWindow': 200000, 'maxTokens': 8192}]}}})
    save(config / 'settings.json', {'quietStartup': True, 'retry': {'enabled': False}, 'enableSkillCommands': True})
    launcher = ([shutil.which('node'), str(args.pi_entry.resolve())] if args.pi_entry else
                [shutil.which('npx.cmd') or shutil.which('npx'), '--yes', '@earendil-works/pi-coding-agent@' + args.pi_version])
    version = subprocess.run([*launcher, '--version'], capture_output=True, text=True, timeout=90, env=env)
    if version.returncode or version.stdout.strip() != args.pi_version:
        raise SystemExit('Pi version check failed; specify --pi-version matching the installed CLI')
    command = [*launcher, '--provider', 'agy-native', '--model', MODEL, '--thinking', 'off', '--mode', 'json', '--print',
               '--no-extensions', '--no-context-files', '--no-prompt-templates', '--no-skills',
               '--session', str(directory / 'session.jsonl')]
    marker = 'PI_' + uuid.uuid4().hex
    hashes = {}
    if args.scenario == 'skills':
        skill = config / 'skills' / 'ledger-digest'
        contract, hashes = skill_fixture(work, skill)
        command += ['--tools', 'read,write,edit,bash', '--skill', str(skill)]
        prompt = ('Implement and validate the ledger digest workflow in this project using the advertised ledger-digest skill, '
                  'its bundled contract and checker. Read and follow the skill. Do not alter the skill, reference or checker. '
                  'Use client tools to do the work, then report the checker result and contract tag.')
    else:
        save(work / 'input.json', {'marker': marker, 'values': [2, 7, 9]})
        if args.scenario == 'mcp':
            if not args.filesystem_server.is_file():
                raise SystemExit('Pass --filesystem-server pointing to the installed official MCP filesystem server entry point')
            save(config / 'mcp.json', {'mcpServers': {'filesystem': {
                'command': shutil.which('node'), 'args': [str(args.filesystem_server.resolve()), str(work)],
                'exposure': 'direct', 'timeout': 30}}})
            command += ['--extension', 'builtin:mcp', '--tools', 'mcp__filesystem__*']
            prompt = ('Use the filesystem MCP tools to read input.json and write output.json in the current workspace. '
                      'Output JSON must contain exactly marker (copied from input) and total (sum of values). '
                      'Read output.json back with MCP to verify. Do not use shell or built-in file tools. Report the marker.')
        else:
            command += ['--tools', 'read,write,edit,bash']
            (work / 'verify.mjs').write_text(
                "import fs from 'node:fs'; import assert from 'node:assert/strict';\n"
                "const a=JSON.parse(fs.readFileSync('input.json')), b=JSON.parse(fs.readFileSync('output.json'));\n"
                "assert.deepEqual(b,{marker:a.marker,total:a.values.reduce((a,b)=>a+b,0)}); console.log('TOOLS_OK');\n")
            prompt = ('Read input.json using the read tool; write output.json using the write tool with exactly marker '
                      '(copied from input) and total initially set to 0. Then use the edit tool to change total to the sum of values. '
                      'Run `node verify.mjs` using bash and fix until it passes. '
                      'Do not change input.json or verify.mjs. Report the marker.')
    (directory / 'prompt.txt').write_text(prompt, encoding='utf-8')
    print(json.dumps({'scenario': args.scenario, 'directory': str(directory), 'session': session_id}), flush=True)
    started = time.monotonic()
    with (directory / 'events.jsonl').open('w', encoding='utf-8') as output:
        child = subprocess.Popen([*command, prompt], cwd=work, env=env, stdout=output,
                                 stderr=subprocess.STDOUT, start_new_session=os.name != 'nt')
        try:
            exit_code = child.wait(timeout=360)
        except BaseException:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            save(directory / 'result.json', {'scenario': args.scenario, 'passed': False, 'interrupted_or_timed_out': True})
            raise
    events = []
    for line in (directory / 'events.jsonl').read_text(encoding='utf-8').splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    calls = [e for e in events if e.get('type') == 'tool_execution_start']
    tool_names = [e.get('toolName') for e in calls]
    errors = [e.get('toolName') for e in events if e.get('type') == 'tool_execution_end' and e.get('isError')]
    assistant_errors = [e for e in events if e.get('type') == 'message_end' and e.get('message', {}).get('stopReason') == 'error']
    result = {'scenario': args.scenario, 'pi_version': args.pi_version, 'exit_code': exit_code,
              'seconds': time.monotonic()-started, 'tool_names': tool_names, 'tool_errors': errors,
              'assistant_errors': len(assistant_errors), 'session_id': session_id}
    if args.scenario == 'skills':
        check = subprocess.run(['node', str(skill / 'scripts/check.mjs'), str(work)], capture_output=True, text=True, encoding='utf-8')
        (directory / 'independent-check.log').write_text(check.stdout + check.stderr, encoding='utf-8')
        result.update(check_passed=check.returncode == 0, skill_unchanged=all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in hashes.items()),
                      skill_read=any('SKILL.md' in json.dumps(e.get('args', {})) for e in calls),
                      reference_read=any('contract.json' in json.dumps(e.get('args', {})) for e in calls),
                      client_ran_checker=any(e.get('toolName') == 'bash' and 'check.mjs' in json.dumps(e.get('args', {})) for e in calls))
        checks = [result[k] for k in ('check_passed', 'skill_unchanged', 'skill_read', 'reference_read', 'client_ran_checker')]
    else:
        output = work / 'output.json'
        result['output_matches'] = output.is_file() and json.loads(output.read_text(encoding='utf-8')) == {'marker': marker, 'total': 18}
        if args.scenario == 'mcp':
            result['mcp_read_write'] = any('filesystem' in n and 'read' in n for n in tool_names) and any('filesystem' in n and 'write' in n for n in tool_names)
            checks = [result['output_matches'], result['mcp_read_write']]
        else:
            result['builtin_read_write_shell'] = all(n in tool_names for n in ('read', 'write', 'edit', 'bash'))
            checks = [result['output_matches'], result['builtin_read_write_shell']]
    result['passed'] = exit_code == 0 and not errors and not assistant_errors and all(checks)
    save(directory / 'result.json', result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
