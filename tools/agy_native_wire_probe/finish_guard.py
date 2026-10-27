"""Turn boundaries for the pinned Linux CLI's structured-output executor.

CLI 1.4.0 scans the whole trajectory for finish, counts forced invocations
across client turns, and can reuse an old structured output. These hooks keep
the CLI's own executor, schema validation, and bounded submission loop. They
do not manufacture a finish call or replay a client request.

See docs/native-backend.md and tests/native_finish_boundary_smoke.py.
"""
import signal
import struct


SUPPORTED_SHA256 = '60e72242f07c131f75afcd9a9e425d6ee097a1a1b3190ea98ae78fa4180bbfa8'
POSTHOOKS = 'google3/third_party/jetski/cortex/executors/posthooks/posthooks.'
HAS_FINISH = POSTHOOKS + 'hasFinishToolCall'
COUNT_FORCED = POSTHOOKS + 'CountForcedInvocations'
FORCE_HOOK = POSTHOOKS + 'NewForceInvocationHookWithOpts.func1'
BUILD_OUTPUT = 'google3/third_party/jetski/cli/steps/steps.BuildJSONOutput'
FUNCTIONS = {
    HAS_FINISH: (0x7f6da40, 0x7f6dac0),
    COUNT_FORCED: (0x7f6dea0, 0x7f6e020),
    FORCE_HOOK: (0x7f6ec60, 0x7f6f120),
    BUILD_OUTPUT: (0x8b29b20, 0x8b2a320),
}
# Offsets and expected instructions are relative to independently resolved
# Go symbols. Verify every site before installing any breakpoint.
SITES = {
    'finish_header': (HAS_FINISH, 0x4f, bytes.fromhex('83380275d6')),
    'finish_false': (HAS_FINISH, 0x5b, bytes.fromhex('31c0c9c3')),
    'count_reset': (COUNT_FORCED, 0x131, bytes.fromhex('488b542428')),
    'count_next': (COUNT_FORCED, 0x22, bytes.fromhex('488b4c242048ffc1')),
    'output_boundary': (BUILD_OUTPUT, 0x3ed, bytes.fromhex('488b5c24404885db')),
    'exhausted': (FORCE_HOOK, 0x405, bytes.fromhex('b80600000031db31')),
}
MAX_OUTPUT_STEPS = 100_000


def validate_symbols(symbols):
    if symbols.get('binary_sha256') != SUPPORTED_SHA256:
        raise ValueError('Finish guard requires the validated CLI binary')
    for name, (start, end) in FUNCTIONS.items():
        if symbols.get('functions', {}).get(name) != {'start': start, 'end': end}:
            raise ValueError('Native symbols lack the validated finish hooks; regenerate symbols')


def install(gdb, *, base, symbols, emit, kill):
    """Install on the private inferior only; an ABI/read failure stops it."""
    validate_symbols(symbols)
    inferior = gdb.selected_inferior()
    addresses = {}
    for kind, (name, offset, expected) in SITES.items():
        address = base + symbols['functions'][name]['start'] + offset
        if bytes(inferior.read_memory(address, len(expected))) != expected:
            raise ValueError('Unexpected finish guard instruction: ' + kind)
        addresses[kind] = address

    def reg(name):
        return int(gdb.parse_and_eval('$' + name))

    def u64(pointer):
        return struct.unpack('<Q', bytes(inferior.read_memory(pointer, 8)))[0]

    def u32(pointer):
        return struct.unpack('<I', bytes(inferior.read_memory(pointer, 4)))[0]

    def setreg(name, value):
        gdb.execute('set $' + name + ' = ' + str(value))

    class FinishBoundary(gdb.Breakpoint):
        def __init__(self, kind):
            super().__init__('*' + hex(addresses[kind]), internal=True)
            self.kind = kind

        def stop(self):
            try:
                if self.kind == 'finish_header':
                    # Header.Type == USER_INPUT. Do not cross it to find an
                    # earlier finish. The normal false epilogue restores SP/BP.
                    if u32(reg('rax')) == 14:
                        setreg('rip', addresses['finish_false'])
                        emit('finish_boundary', action='current_turn_required')
                elif self.kind == 'count_reset':
                    # Native code has already identified a user input that is
                    # NOT either of its two forced-invocation reminder strings.
                    # Keep counting reminders, resetting only at real input.
                    if u64(reg('rsp') + 0x28):
                        setreg('rdx', 0)
                        setreg('rip', addresses['count_next'])
                elif self.kind == 'output_boundary':
                    # BuildJSONOutput needs the full history for cumulative
                    # usage/num_turns. Clear only its stale JSON accumulator,
                    # on its own stack, before it assigns the result object.
                    sp = reg('rsp')
                    if not u64(sp + 0x40):
                        emit('finish_output_checked', cli_turns=reg('r15'))
                        return False
                    steps, length = u64(sp + 0x138), u64(sp + 0x140)
                    if length > MAX_OUTPUT_STEPS:
                        raise ValueError('Invalid native output step count')
                    current_finish = False
                    for index in range(length - 1, -1, -1):
                        step = u64(steps + index * 8)
                        if not step:
                            continue
                        step_type = u32(step + 8)
                        if step_type == 2:
                            current_finish = True
                            break
                        if step_type == 14:
                            break
                    if not current_finish:
                        setreg('rdi', 0)
                        inferior.write_memory(sp + 0x40, bytes(16))
                        emit('finish_boundary', action='stale_output_cleared')
                    # Same FIFO as finish_exhausted: this marker lets the
                    # consumer drain it even if stdout overtakes guard events.
                    emit('finish_output_checked', cli_turns=reg('r15'))
                elif self.kind == 'exhausted':
                    # CLI otherwise reports SUCCESS even with no current
                    # submission. The API must reject this after draining
                    # captured usage, without another full-history replay.
                    emit('finish_exhausted', attempts=reg('r8'), limit=reg('rdx'))
            except Exception as exc:
                emit('hook_error', stage='finish_guard', code='native_finish_guard_failed',
                     error=type(exc).__name__)
                kill(inferior.pid, signal.SIGKILL)
            return False

    return [FinishBoundary(kind) for kind in
            ('finish_header', 'count_reset', 'output_boundary', 'exhausted')]
