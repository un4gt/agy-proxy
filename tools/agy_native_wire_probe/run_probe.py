import json
import os
import signal
import subprocess
import time
import threading
import ctypes
from pathlib import Path

os.chdir('/workspace-empty')
assert ctypes.CDLL(None).prctl(36,1,0,0,0)==0  # Reap CLI after debugger detaches at exit_group.
args=['/root/.local/bin/agy','--model','gemini-3.8-flash-low','--effort','low',
      '--output-format','stream-json','--print-timeout','90s',
      '-p','Reply with exactly NATIVE_WIRE_OK_20261018. Do not use tools.']
results=[]
for mode in ('baseline','hooked'):
    packet=subprocess.Popen(['tcpdump','-i','eth0','-U','-w',f'/tmp/{mode}.pcap','tcp'],stdout=subprocess.DEVNULL,stderr=open(f'/tmp/{mode}-tcpdump.log','w'))
    time.sleep(.5)
    command=args if mode=='baseline' else ['gdb','-q','-batch','-x','/evidence/probe_gdb.py','--args',*args]
    target=Path(f'/evidence/{mode}.driver.log' if mode=='baseline' else '/evidence/hooked.stdout.jsonl')
    target.write_text('')
    stop=threading.Event()
    timeline=[]
    def monitor():
        with target.open() as file:
            while True:
                line=file.readline()
                if line:
                    try:
                        event=json.loads(line)
                        timeline.append({'event':event.get('event'),'monotonic':time.monotonic()})
                    except ValueError:pass
                elif stop.is_set():break
                else:time.sleep(.005)
    reader=threading.Thread(target=monitor)
    reader.start()
    start=time.monotonic()
    try:
        with open(f'/evidence/{mode}.driver.log','w') as stdout,open(f'/evidence/{mode}.driver.stderr.log','w') as stderr:
            child=subprocess.Popen(command,stdout=stdout,stderr=stderr,start_new_session=True)
            try:code=child.wait(timeout=115)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGTERM)
                code=child.wait(timeout=10)
        inferior_exit=None
        inferior_gone=None
        if mode=='hooked' and code==0:
            events=[json.loads(line) for line in Path('/evidence/hook-events.jsonl').read_text().splitlines()]
            pid=next(item['pid'] for item in events if item['kind']=='probe_ready')
            try:
                reaped,status=os.waitpid(pid,0)
                inferior_exit=os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                pass  # GDB may already have reaped the process before quitting.
            inferior_gone=not Path(f'/proc/{pid}').exists()
        results.append({'mode':mode,'exit':code,'reaped_inferior_exit':inferior_exit,'inferior_gone':inferior_gone,'seconds':round(time.monotonic()-start,3)})
    finally:
        stop.set();reader.join(timeout=2)
        Path(f'/evidence/{mode}.timeline.json').write_text(json.dumps(timeline,indent=2))
        packet.send_signal(signal.SIGINT)
        packet.wait(timeout=5)
    print(json.dumps(results[-1]),flush=True)
Path('/evidence/runs.json').write_text(json.dumps(results,indent=2))
