import gdb
import json
import os
import shlex
import sys
import traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,'/tmp/probe-deps')
sys.path.insert(0,'/opt/probe-deps')
from observe import capture, capture_write, emit, cleaned_requests

gdb.execute('set pagination off')
gdb.execute('set confirm off')
gdb.execute('set print thread-events off')
gdb.execute('set debuginfod enabled off')
gdb.execute('handle SIGURG nostop noprint pass')
gdb.execute('handle SIGPIPE nostop noprint pass')
redirects=' > '+shlex.quote(os.environ.get('AGY_WIRE_STDOUT','/evidence/hooked.stdout.jsonl'))
redirects+=' 2> '+shlex.quote(os.environ.get('AGY_WIRE_STDERR','/evidence/hooked.stderr.log'))
if os.environ.get('AGY_WIRE_STDIN'):
    redirects+=' < '+shlex.quote(os.environ['AGY_WIRE_STDIN'])
gdb.execute('set args '+gdb.parameter('args')+redirects)
gdb.execute('starti')
pid=gdb.selected_inferior().pid
emit('inferior_started',pid=pid)
rows=Path(f'/proc/{pid}/maps').read_text().splitlines()
binary=os.environ.get('AGY_WIRE_BINARY','/root/.local/bin/agy')
row=next(row for row in rows if binary in row and row.split()[2]=='00000000')
base=int(row.split('-')[0],16)
symbols=json.loads(Path(os.environ.get('AGY_WIRE_SYMBOLS','/evidence/symbols.json')).read_text())
pending={}
pending_writes={}
def reg(name):return int(gdb.parse_and_eval('$'+name))
class InputProbe(gdb.Breakpoint):
    def stop(self):
        try:
            p,n=reg('rax'),reg('rbx')
            if 0<n<=16*1024*1024:
                data=bytes(gdb.selected_inferior().read_memory(p,n))
                try: doc=json.loads(data)
                except (ValueError,UnicodeError): return False
                if isinstance(doc,dict) and doc.get('requestType') == 'agent':
                    if os.environ.get('AGY_WIRE_INPUT_CLEAN') == '1':
                        from input_clean import rewrite_buffer, digest
                        if digest(doc) not in cleaned_requests:
                            replacement = rewrite_buffer(data)
                            gdb.selected_inferior().write_memory(p,replacement)
                            cleaned_requests.add(digest(json.loads(replacement)))
                            emit('input_cleaned',bytes=n)
        except Exception as exc:
            emit('hook_error',stage='input_probe',error=type(exc).__name__)
            # Stop the child before a malformed/unknown input can be sent.
            import signal
            os.kill(pid,signal.SIGKILL)
        return False

if os.environ.get('AGY_WIRE_INPUT_CLEAN') == '1':
    from input_clean import SUPPORTED_SHA256
    if symbols['binary_sha256'] != SUPPORTED_SHA256:
        raise RuntimeError('Input cleanup requires the validated CLI binary')
    for name in ('encoding/json/v2.Marshal','google3/third_party/golang/protobuf/v2/encoding/protojson/protojson.MarshalOptions.marshal'):
        symbol=symbols['functions'][name]
        instructions=gdb.selected_inferior().architecture().disassemble(base+symbol['start'],base+symbol['end'])
        for ins in instructions:
            if ins['asm'].startswith('ret'): InputProbe('*'+hex(ins['addr']),internal=True)

class Entry(gdb.Breakpoint):
    def __init__(self,name):
        self.name=name
        start=base+symbols['functions'][name]['start']
        instructions=gdb.selected_inferior().architecture().disassemble(start,start+32)
        guard=next(i for i,ins in enumerate(instructions) if ins['asm'].startswith('jbe'))
        # Stop AFTER the Go stack-growth branch, otherwise morestack re-entry
        # observes the same write twice and corrupts HTTP/2 stream accounting.
        super().__init__('*'+hex(instructions[guard+1]['addr']),internal=True)
    def stop(self):
        try:
            conn,p,n=reg('rax'),reg('rbx'),reg('rcx')
            if self.name.endswith('.Write'):
                if 0<n<=16*1024*1024:
                    pending_writes[reg('r14')]=(conn,bytes(gdb.selected_inferior().read_memory(p,n)))
            else:
                pending[reg('r14')]=(conn,p,n)
        except Exception as exc:emit('hook_error',stage='entry',error=type(exc).__name__,frames=[f'{f.name}:{f.lineno}' for f in traceback.extract_tb(exc.__traceback__)])
        return False
class Return(gdb.Breakpoint):
    def stop(self):
        try:
            state=pending.pop(reg('r14'),None)
            if state:
                conn,p,capacity=state
                n=reg('rax')
                if 0<n<=capacity:capture(hex(conn),'recv',bytes(gdb.selected_inferior().read_memory(p,n)))
        except Exception as exc:emit('hook_error',stage='return',error=type(exc).__name__,frames=[f'{f.name}:{f.lineno}' for f in traceback.extract_tb(exc.__traceback__)])
        return False

class WriteReturn(gdb.Breakpoint):
    def stop(self):
        try:
            state=pending_writes.pop(reg('r14'),None)
            if state:
                conn,data=state
                n=reg('rax')
                capture_write(hex(conn),data,n)
        except Exception as exc:emit('hook_error',stage='write_return',error=type(exc).__name__,frames=[f'{f.name}:{f.lineno}' for f in traceback.extract_tb(exc.__traceback__)])
        return False

for name in ('crypto/tls.(*Conn).Write','crypto/tls.(*Conn).Read'):Entry(name)
read=symbols['functions']['crypto/tls.(*Conn).Read']
instructions=gdb.selected_inferior().architecture().disassemble(base+read['start'],base+read['end'])
returns=[ins['addr'] for ins in instructions if ins['asm'].startswith('ret')]
for address in returns:Return('*'+hex(address),internal=True)
write=symbols['functions']['crypto/tls.(*Conn).Write']
instructions=gdb.selected_inferior().architecture().disassemble(base+write['start'],base+write['end'])
for ins in instructions:
    if ins['asm'].startswith('ret'):WriteReturn('*'+hex(ins['addr']),internal=True)
emit('probe_ready',pid=pid,binary_sha256=symbols['binary_sha256'],read_return_sites=len(returns),native_tls=True)
def on_exit_syscall():
    emit('cli_exit',code=reg('rdi'))
    for breakpoint in list(gdb.breakpoints()):
        if isinstance(breakpoint,(Entry,Return,WriteReturn)):
            breakpoint.delete()
gdb.execute('catch syscall exit_group')
catch=gdb.breakpoints()[-1]
gdb.execute(f'commands {catch.number}\nsilent\npython on_exit_syscall()\ndetach\nquit\nend')
gdb.execute('continue')
emit('probe_end',pending_reads=len(pending))
