"""Real local Chat route: cleaned input, same ID/different owners, cancellation."""
import asyncio
import json
from pathlib import Path
import secrets
import tempfile

import httpx

from app import create_app
from app.config import Config
from app.session_context import fingerprint


async def main():
    report = {'passed': False}
    keys = ['fixture-owner-a', 'fixture-owner-b']
    markers = {key: 'PRIVATE_' + secrets.token_hex(12) for key in keys}
    history = {key: [{'role': 'user', 'content': 'Remember this private marker and reply with exactly it: ' + markers[key]}] for key in keys}
    pids = []
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(agy_bin='/root/.local/bin/agy', data_dir=directory,
                     native_chat=True, native_input_clean=True, api_keys=tuple(keys),
                     password='local-fixture', turn_timeout=120)
        app = create_app(cfg)
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
                    async def turn(key, prompt=None):
                        if prompt:
                            history[key].append({'role': 'user', 'content': prompt})
                        response = await client.post('/v1/chat/completions', headers={
                            'Authorization': 'Bearer ' + key, 'X-Agy-Session-Id': 'same-external-id'},
                            json={'model': 'gemini-3.8-flash-low', 'tools': [], 'messages': history[key]})
                        assert response.status_code == 200, response.text
                        message = response.json()['choices'][0]['message']
                        assert message['content'].strip() == markers[key], message
                        history[key].append(message)

                    def active():
                        return {s.client_context.owner: s for s in app.state.native_sessions._sessions}

                    await asyncio.gather(*(turn(key) for key in keys))
                    sessions = active()
                    assert len(sessions) == 2
                    pids = [s.observer.inferior_pid for s in sessions.values()]
                    conversations = [s.conversation_id for s in sessions.values()]
                    assert len(set(pids)) == len(set(conversations)) == 2
                    await asyncio.gather(*(turn(key, 'Reply with only my private marker again.') for key in reversed(keys)))
                    assert all(active()[owner] is session for owner, session in sessions.items())
                    a = sessions[fingerprint(keys[0])]
                    b = sessions[fingerprint(keys[1])]
                    task = asyncio.create_task(turn(keys[0], 'Write 2000 long numbered sentences about trees.'))
                    cancelled = [a.proc.pid, a.observer.inferior_pid]
                    try:
                        async with asyncio.timeout(30):
                            while a.observer.turns != 3 or not a.observer.lock.locked():
                                await asyncio.sleep(.05)
                        await asyncio.sleep(.5)
                        survivor = asyncio.create_task(turn(keys[1], 'Reply with only my private marker again.'))
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        await survivor
                        assert all(not Path(f'/proc/{pid}').exists() for pid in cancelled)
                        assert fingerprint(keys[0]) not in active()
                        assert active()[fingerprint(keys[1])] is b and b.turns == 3
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    report.update(passed=True, separate_pids=pids, separate_conversations=conversations,
                                  same_external_id=True, two_owner_recall=True,
                                  cancelled_processes_gone=True, survivor_turns=b.turns)
        finally:
            report['all_processes_gone'] = all(not Path(f'/proc/{pid}').exists() for pid in pids)
            Path('/evidence/clean-sessions.json').write_text(json.dumps(report, indent=2))
            print(json.dumps(report), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
