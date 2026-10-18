"""Compare live text and external-tool finish payloads in the same CLI call."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.external_tools import ExternalTools
from tools.agy_native_wire_probe.runtime import NativeSession


async def collect(session, prompt):
    events = [event async for event in session.turn(prompt)]
    result = events[-1]
    assert result['kind'] == 'result'
    result['text_chunks'] = sum(e['kind'] == 'text' for e in events)
    return result


def configure(root, bridge):
    agents = root / '.agents' / 'agents'
    agents.mkdir(parents=True)
    (agents / 'proxy-external.md').write_text(
        '---\nname: proxy-external\ndescription: Local native bridge test\n'
        'tools: [finish]\nmainAgent: true\nsubagent: false\ncommandExecutionPolicy: off\n---\n'
        + bridge.session_instructions())
    (root / '.agents' / 'hooks.json').write_text(json.dumps({
        'proxy-external-tools': {'PreToolUse': [{'matcher': '*', 'hooks': [{
            'type': 'command', 'command': f'{sys.executable} /repo/app/tool_guard.py', 'timeout': 10}]}]}}))
    schema = root / 'schema.json'
    schema.write_text(json.dumps(bridge.schema))
    return ['--agent', 'proxy-external', '--disable-slash-commands', '--json-schema', str(schema)]


async def main():
    report = {'passed': False}
    try:
        async with NativeSession() as session:
            result = await collect(session, 'Write exactly 60 numbered lines about trees, one full sentence of at least 12 words per line. Do not use tools.')
            assert result['text'].strip() == result['cli']['response'].strip()
            assert result['text_chunks'] > 1
            report['long_text'] = result
            print(json.dumps({'long_text': result['metrics'], 'text_chunks': result['text_chunks']}), flush=True)

        body = {'tools': [{'type': 'function', 'function': {'name': 'lookup_local_marker',
                'description': 'Get the marker from the client. Only the external client can execute this tool.',
                'parameters': {'type': 'object', 'properties': {'key': {'type': 'string'}},
                               'required': ['key'], 'additionalProperties': False}}}],
                'messages': [{'role': 'user', 'content': 'Use lookup_local_marker with key="sample". Wait for the real result and then reply with exactly its marker.'}]}
        bridge = ExternalTools.from_body(body)
        with tempfile.TemporaryDirectory(prefix='agy-native-tools-') as directory:
            extra = configure(Path(directory), bridge)
            async with NativeSession(cwd=directory, extra=extra) as session:
                first = await collect(session, bridge.prompt(bridge.messages, fresh=True))
                checked = bridge.result(first['cli'])
                assert len(checked.calls) == 1
                call = checked.calls[0]
                assert call.name == 'lookup_local_marker' and call.arguments == {'key': 'sample'}
                assert first['calls'] and all(c['name'] == 'finish' for c in first['calls'])
                report['tool_request'] = first
                body['messages'] += [
                    {'role': 'assistant', 'content': checked.text, 'tool_calls': [
                        {'id': call.id, 'type': 'function', 'function': {'name': call.name, 'arguments': json.dumps(call.arguments)}}]},
                    {'role': 'tool', 'tool_call_id': call.id, 'content': '{"marker":"CLIENT_RESULT_5729"}'},
                ]
                continuation = ExternalTools.from_body(body)
                second = await collect(session, continuation.prompt(continuation.messages[-2:], fresh=False))
                checked = continuation.result(second['cli'])
                assert checked.text.strip() == 'CLIENT_RESULT_5729' and not checked.calls
                assert all(c['name'] == 'finish' for c in second['calls'])
                report['tool_result'] = second
                print(json.dumps({'tool_request': first['metrics'], 'tool_result': second['metrics']}), flush=True)
        report['passed'] = True
    finally:
        Path('/evidence/live-content.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({'passed': report['passed']}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
