"""Validate native turn boundaries and failure delivery independently of GDB."""
import asyncio
import struct
import time
from types import SimpleNamespace

import pytest

from app.agy import AgyError
from app.native_chat import captured_output
from tools.agy_native_wire_probe import finish_guard as guard
from tools.agy_native_wire_probe.runtime import NativeSession, WireError


def debugger():
    memory, registers, events, kills, breakpoints = {}, {}, [], [], []
    base = 0x100000000
    symbols = {'binary_sha256': guard.SUPPORTED_SHA256, 'functions': {
        name: {'start': start, 'end': end} for name, (start, end) in guard.FUNCTIONS.items()}}

    def write(pointer, data):
        memory.update({pointer + i: value for i, value in enumerate(data)})

    def read(pointer, length):
        return bytes(memory[pointer + i] for i in range(length))

    def execute(command):
        name, value = command.removeprefix('set $').split(' = ')
        registers[name] = int(value)

    class Breakpoint:
        def __init__(self, spec, **_):
            self.address = int(spec[1:], 16)
            breakpoints.append(self)

    for name, offset, expected in guard.SITES.values():
        write(base + symbols['functions'][name]['start'] + offset, expected)
    fake = SimpleNamespace(Breakpoint=Breakpoint, parse_and_eval=lambda name: registers[name[1:]],
                           execute=execute, selected_inferior=lambda: SimpleNamespace(
                               read_memory=read, write_memory=write, pid=999))
    return SimpleNamespace(gdb=fake, base=base, symbols=symbols, registers=registers,
                           write=write, read=read, events=events, kills=kills, breakpoints=breakpoints,
                           emit=lambda kind, **kw: events.append({'kind': kind, **kw}),
                           kill=lambda *args: kills.append(args))


def install(d):
    return {bp.kind: bp for bp in guard.install(d.gdb, base=d.base, symbols=d.symbols,
                                               emit=d.emit, kill=d.kill)}


@pytest.mark.parametrize('bad', ['sha', 'missing_symbol', 'wrong_address', 'instruction'])
def test_unknown_abi_never_installs_partial_hooks(bad):
    d = debugger()
    if bad == 'sha':
        d.symbols['binary_sha256'] = 'unvalidated'
    elif bad == 'missing_symbol':
        del d.symbols['functions'][guard.BUILD_OUTPUT]
    elif bad == 'wrong_address':
        d.symbols['functions'][guard.COUNT_FORCED]['start'] += 1
    else:
        name, offset, _ = guard.SITES['exhausted']
        d.write(d.base + d.symbols['functions'][name]['start'] + offset, b'\0')
    with pytest.raises(ValueError):
        install(d)
    assert not d.breakpoints


@pytest.mark.parametrize('step_type,changed', [(14, True), (15, False), (2, False)])
def test_finish_scan_stops_at_input_without_changing_real_finish(step_type, changed):
    d = debugger()
    hooks = install(d)
    d.registers.update(rax=1000, rip=2000, rsp=3000, rbx=4)
    d.write(1000, struct.pack('<I', step_type))
    assert hooks['finish_header'].stop() is False
    expected_pc = d.base + guard.FUNCTIONS[guard.HAS_FINISH][0] + 0x5b
    assert d.registers == dict(rax=1000, rip=expected_pc if changed else 2000, rsp=3000, rbx=4)
    assert not d.kills


def test_forced_count_resets_at_real_user_input():
    d = debugger()
    hooks = install(d)
    d.registers.update(rsp=1000, rdx=7, rip=2000)
    d.write(1000 + 0x28, struct.pack('<Q', 3))
    hooks['count_reset'].stop()
    assert d.registers['rdx'] == 0
    assert d.registers['rip'] == d.base + guard.FUNCTIONS[guard.COUNT_FORCED][0] + 0x22
    assert not d.kills


@pytest.mark.parametrize('types,cleared', [
    ([14, 15, 2], False), ([14, 15, 2, 14, 15], True),
    ([14, 15, 2, 14, 15, 14, 15], True), ([14, 15, 2, 14, 15, 2], False),
    ([14, None, 15], True), ([], True),
])
def test_output_never_borrows_finish_across_current_input(types, cleared):
    d = debugger()
    hooks = install(d)
    d.registers.update(rsp=1000, rdi=9000, r15=7, r10=120)
    d.write(1000 + 0x40, struct.pack('<QQ', 30, 32))
    d.write(1000 + 0x138, struct.pack('<QQ', 2000, len(types)))
    for i, kind in enumerate(types):
        pointer = 3000 + i * 100 if kind is not None else 0
        d.write(2000 + i * 8, struct.pack('<Q', pointer))
        if pointer:
            d.write(pointer + 8, struct.pack('<I', kind))
    hooks['output_boundary'].stop()
    assert d.read(1000 + 0x40, 16) == (bytes(16) if cleared else struct.pack('<QQ', 30, 32))
    assert d.registers == dict(rsp=1000, rdi=0 if cleared else 9000, r15=7, r10=120)
    assert d.events[-1] == {'kind': 'finish_output_checked', 'cli_turns': 7}
    assert not d.kills


def test_memory_failure_stops_the_inferior(monkeypatch):
    monkeypatch.setattr(guard.signal, 'SIGKILL', 9, raising=False)
    d = debugger()
    hooks = install(d)
    d.registers['rax'] = 1000  # Deliberately unmapped.
    hooks['finish_header'].stop()
    assert d.kills == [(999, 9)]
    assert d.events[-1]['code'] == 'native_finish_guard_failed'


@pytest.mark.parametrize('stdout_first', [True, False])
@pytest.mark.parametrize('marker', ['current', 'stale', 'missing'])
def test_guard_events_are_drained_before_accepting_stdout(monkeypatch, stdout_first, marker):
    import tools.agy_native_wire_probe.runtime as runtime
    monkeypatch.setattr(runtime, 'CAPTURE_DRAIN_TIMEOUT', .01)

    async def check():
        session = NativeSession(guard_finish=True, timeout=1)
        session.proc = SimpleNamespace(returncode=None)
        session.conversation_id = 'fixture'

        async def close():
            session.closed = True

        async def write(_):
            now = time.monotonic()
            wire = [
                {'kind': 'generation_request', 'request_type': 'agent', 'request_id': '1'},
                {'kind': 'sse', 'request_id': '1', 'candidates': [
                    {'finishReason': 'STOP', 'parts': [{'text': 'unsubmitted'}]}],
                 'usage': {'promptTokenCount': 20, 'candidatesTokenCount': 5, 'totalTokenCount': 25}},
                {'kind': 'message_complete', 'request_id': '1', 'direction': 'recv'},
                {'kind': 'finish_exhausted', 'attempts': 3, 'limit': 3},
            ]
            if marker != 'missing':
                wire.append({'kind': 'finish_output_checked', 'cli_turns': 1 if marker == 'current' else 0})
            stdout = [('stdout', {'event': 'result', 'observed_at': now, 'result': {
                'status': 'SUCCESS', 'conversation_id': 'fixture', 'num_turns': 1}})]
            wire = [('events', dict(e, monotonic=now)) for e in wire]
            for row in stdout + wire if stdout_first else wire + stdout:
                await session.queue.put(row)

        session.close, session._write = close, write
        if marker == 'current':
            events = [event async for event in session.turn('fixture')]
            result = events[-1]
            assert result['metrics']['finish_exhausted']
            assert result['usage']['total_tokens'] == 25
            assert result['usage_complete']
            with pytest.raises(AgyError) as caught:
                captured_output(result)
            assert caught.value.code == 'native_finish_exhausted'
            assert not caught.value.retryable
        else:
            with pytest.raises(WireError) as caught:
                async for _ in session.turn('fixture'):
                    pass
            assert caught.value.code == 'native_capture_incomplete'
            assert session.closed
    asyncio.run(check())
