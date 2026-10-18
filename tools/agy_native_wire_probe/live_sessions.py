"""Local-only native transport session checks; sends real upstream requests."""
import asyncio
import json
import os
from functools import partial
from pathlib import Path
import secrets
import sys
from contextlib import aclosing

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.agy_native_wire_probe.sessions import NativeSessions
from tools.agy_native_wire_probe.runtime import NativeSession


async def collect(session, prompt):
    events = [event async for event in session.turn(prompt)]
    result = events[-1]
    assert result['kind'] == 'result'
    return result


async def main():
    report = {'passed': False}
    sessions = NativeSessions(factory=partial(
        NativeSession, binary=os.environ.get('AGY_WIRE_BINARY', '/root/.local/bin/agy')))
    markers = {owner: 'SESSION_' + secrets.token_hex(8) for owner in ('owner-a', 'owner-b')}
    pids = {}
    conversations = {}

    async def turn(owner, recall=False):
        async with sessions.lease(owner, 'same-external-session-id') as session:
            pids[owner] = session.inferior_pid
            prompt = ('What private marker did I give you? Reply with exactly that marker. Do not use tools.' if recall else
                      f'Remember this private marker: {markers[owner]}. Reply with exactly the marker. Do not use tools.')
            result = await collect(session, prompt)
            assert result['text'].strip() == markers[owner], result['text']
            assert result['cli']['response'].strip() == result['text'].strip()
            conversations[owner] = session.conversation_id
            print(json.dumps({'owner': owner, 'metrics': result['metrics']}), flush=True)
            return result

    try:
        report['initial'] = await asyncio.gather(turn('owner-a'), turn('owner-b'))
        assert pids['owner-a'] != pids['owner-b']
        assert conversations['owner-a'] != conversations['owner-b']
        original_pids, original_conversations = dict(pids), dict(conversations)
        report['recall'] = await asyncio.gather(turn('owner-b', True), turn('owner-a', True))
        assert pids == original_pids and conversations == original_conversations
        report['pids'], report['conversations'] = pids, conversations

        streaming = asyncio.Event()
        cancelled_pids = []

        async def long_turn():
            async with sessions.lease('owner-a', 'same-external-session-id') as session:
                cancelled_pids.extend([session.proc.pid, session.inferior_pid])
                async with aclosing(session.turn('Write 300 numbered lines about trees, with one full sentence per line. Do not use tools.')) as events:
                    async for event in events:
                        if event['kind'] == 'text':
                            streaming.set()
                            await asyncio.Event().wait()

        task = asyncio.create_task(long_turn())
        try:
            async with asyncio.timeout(120):
                await streaming.wait()
            survivor = asyncio.create_task(turn('owner-b', True))
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            report['survivor'] = await survivor
            assert all(not Path(f'/proc/{pid}').exists() for pid in cancelled_pids)
            assert sessions.key('owner-a', 'same-external-session-id') not in sessions.entries
            report['cancelled_processes_gone'] = True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        report['passed'] = True
    finally:
        await sessions.close()
        report['all_processes_gone'] = all(not Path(f'/proc/{pid}').exists() for pid in pids.values())
        Path('/evidence/live-sessions.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({'passed': report['passed'], 'all_processes_gone': report['all_processes_gone']}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
