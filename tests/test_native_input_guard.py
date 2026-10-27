"""The CLI's per-part byte truncation must not corrupt proxy history replay."""
import ast
import asyncio
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agy import AgyError
from app.config import Config
from app.native_chat import NativeAgySession
from tools.agy_native_wire_probe.input_guard import (
    CLI_INPUT_LIMIT, MAX_INPUT_BYTES, PREFIX_BYTES, PROXY_PREFIXES,
    InputLimitError, preserved_limit,
)
from tools.agy_native_wire_probe.runtime import NativeSession


@pytest.mark.parametrize('prefix', PROXY_PREFIXES)
@pytest.mark.parametrize('length', [192_001, 214_358, 500_000, MAX_INPUT_BYTES])
def test_preserve_complete_and_incremental_proxy_envelopes(prefix, length):
    assert preserved_limit(prefix, length, CLI_INPUT_LIMIT) == length


@pytest.mark.parametrize('prefix,length,limit', [
    (PROXY_PREFIXES[0], CLI_INPUT_LIMIT, CLI_INPUT_LIMIT),
    (PROXY_PREFIXES[0], 100, CLI_INPUT_LIMIT),
    (PROXY_PREFIXES[0], 214358, 1000),
    (b'<USER_REQUEST>\nordinary CLI input', 214358, CLI_INPUT_LIMIT),
    (b'<System Prompt>\n', 214358, CLI_INPUT_LIMIT),
    (b'[Complete client conversation]\n', 214358, CLI_INPUT_LIMIT),
    (b'prefix ' + PROXY_PREFIXES[0], 214358, CLI_INPUT_LIMIT),
])
def test_do_not_change_other_cli_truncation(prefix, length, limit):
    assert preserved_limit(prefix, length, limit) is None


def test_capture_bound_is_not_disabled():
    with pytest.raises(InputLimitError):
        preserved_limit(PROXY_PREFIXES[0], MAX_INPUT_BYTES + 1, CLI_INPUT_LIMIT)


@pytest.mark.parametrize('case', ['protected', 'ordinary', 'oversize', 'read_failure'])
def test_gdb_guard_changes_only_the_limit_and_stops_on_failure(monkeypatch, case):
    # Execute the production callback with a tiny GDB double; do not execute
    # probe startup, signals, processes or real memory writes in this test.
    import tools.agy_native_wire_probe.input_guard as guard
    monkeypatch.setitem(sys.modules, 'input_guard', guard)
    monkeypatch.setattr(signal, 'SIGKILL', 9, raising=False)
    path = Path(__file__).parents[1] / 'tools/agy_native_wire_probe/probe_gdb.py'
    nodes = [node for node in ast.parse(path.read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in {'reg', 'InputLimitProbe'}]
    registers = {'rax': 12345, 'rbx': MAX_INPUT_BYTES + 1 if case == 'oversize' else 214358,
                 'rcx': CLI_INPUT_LIMIT}
    commands, reads, events, kills = [], [], [], []

    def read_memory(pointer, length):
        reads.append((pointer, length))
        if case == 'read_failure':
            raise RuntimeError('memory unavailable')
        prefix = b'ordinary input' if case == 'ordinary' else PROXY_PREFIXES[0]
        return prefix.ljust(length, b'x')

    fake = SimpleNamespace(Breakpoint=object, parse_and_eval=lambda reg: registers[reg[1:]],
                           selected_inferior=lambda: SimpleNamespace(read_memory=read_memory),
                           execute=commands.append)
    env = {'gdb': fake, 'emit': lambda kind, **kw: events.append(dict(kind=kind, **kw)),
           'os': SimpleNamespace(kill=lambda pid, sig: kills.append(pid)), 'pid': 999,
           'traceback': __import__('traceback')}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), env)
    assert env['InputLimitProbe']().stop() is False
    assert reads == [(12345, PREFIX_BYTES)]
    assert registers == {'rax': 12345, 'rbx': MAX_INPUT_BYTES + 1 if case == 'oversize' else 214358,
                         'rcx': CLI_INPUT_LIMIT}
    if case == 'protected':
        assert commands == ['set $rcx = 214358']
        assert events == [{'kind': 'input_preserved', 'bytes': 214358, 'previous_limit': 192000}]
        assert not kills
    elif case == 'ordinary':
        assert not commands and not events and not kills
    else:
        assert not commands and kills == [999]
        assert events[0]['code'] == ('native_input_too_large' if case == 'oversize' else 'native_input_guard_failed')


def test_serialized_input_above_capture_bound_stops_before_reading(monkeypatch):
    # JSON escaping, tools and multiple parts can exceed the bound even when
    # each preserved user part is individually within the limit.
    import tools.agy_native_wire_probe.input_guard as guard
    monkeypatch.setitem(sys.modules, 'input_guard', guard)
    monkeypatch.setattr(signal, 'SIGKILL', 9, raising=False)
    path = Path(__file__).parents[1] / 'tools/agy_native_wire_probe/probe_gdb.py'
    nodes = [node for node in ast.parse(path.read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in {'reg', 'InputProbe'}]
    registers = {'rax': 12345, 'rbx': MAX_INPUT_BYTES + 1}
    events, kills = [], []

    def read_memory(*args):
        pytest.fail('Oversized serialized input must be rejected before reading memory')

    fake = SimpleNamespace(Breakpoint=object, parse_and_eval=lambda reg: registers[reg[1:]],
                           selected_inferior=lambda: SimpleNamespace(read_memory=read_memory))
    env = {'gdb': fake, 'emit': lambda kind, **kw: events.append(dict(kind=kind, **kw)),
           'os': SimpleNamespace(kill=lambda pid, sig: kills.append(pid)), 'pid': 999,
           'traceback': __import__('traceback')}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), env)
    assert env['InputProbe']().stop() is False
    assert kills == [999]
    assert len(events) == 1
    assert events[0]['kind'] == 'hook_error'
    assert events[0]['stage'] == 'input_probe'
    assert events[0]['code'] == 'native_input_too_large'


@pytest.mark.parametrize('code,status', [('native_input_user_metadata_invalid', 502),
                                        ('native_input_guard_failed', 502),
                                        ('native_input_cleanup_failed', 502),
                                        ('native_input_too_large', 413)])
def test_hook_diagnostic_reaches_api_error_code(code, status):
    async def turn(prompt):
        NativeSession._check_failure('events', {'kind': 'hook_error', 'stage': 'input_probe', 'code': code})
        yield  # pragma: no cover

    async def check():
        session = NativeAgySession(Config(), model='auto', effort=None)
        session.observer = SimpleNamespace(turn=turn)
        with pytest.raises(AgyError) as caught:
            async for _ in session.send_turn('fixture'):
                pass
        assert caught.value.code == code
        assert caught.value.status_code == status
        assert not caught.value.retryable
    asyncio.run(check())
