"""Experimental Linux native-CLI observer, with a live FIFO event channel.

The CLI owns all network traffic. stdout confirms lifecycle; TLS observations
provide content. This module is not enabled by the application's default path.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import tempfile
import time


class WireError(RuntimeError):
    pass


class NativeSession:
    def __init__(self, *, model="gemini-3.8-flash-low", cwd="/workspace-empty",
                 extra=(), symbols="/evidence/symbols.json", timeout=120,
                 binary="/root/.local/bin/agy", cli_args=None, environment=None, yield_cli_steps=False, clean_input=False):
        self.model, self.cwd, self.extra = model, cwd, list(extra)
        self.symbols, self.timeout = symbols, timeout
        self.directory = None
        self.proc = None
        self.inferior_pid = None
        self.tasks = []
        self.transports = []
        self.descriptors = []
        self.queue = asyncio.Queue(maxsize=1024)
        self.lock = asyncio.Lock()
        self.turns = 0
        self.conversation_id = None
        self.closed = False
        self.last_metrics = None
        self._usage_totals = {}
        self.workspace = None
        self.binary = binary
        self.cli_args = cli_args
        self.environment = environment
        self.yield_cli_steps = yield_cli_steps
        self.clean_input = clean_input

    async def start(self):
        if self.closed or self.directory is not None:
            raise WireError("Native session cannot be started twice")
        try:
            return await self._start()
        except BaseException:
            await self.close()
            raise

    async def _start(self):
        if os.name != "posix":
            raise WireError("Native observation prototype requires Linux")
        environment = self.environment if self.environment is not None else os.environ
        if environment.get("CLOUD_CODE_URL"):
            raise WireError("The native transport probe requires the original upstream URL")
        ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)
        self.directory = tempfile.TemporaryDirectory(prefix="agy-native-session-")
        root = Path(self.directory.name)
        # A private working directory prevents project state leaking across CLI
        # instances. Explicit cwd is reserved for a caller-owned restricted agent.
        if self.cwd == "/workspace-empty":
            self.workspace = root / "workspace"
            self.workspace.mkdir()
            self.cwd = str(self.workspace)
        binary = str(root / "agy")
        # CLI auto-update replaces its installed executable. Pin a private
        # snapshot before validation so another session cannot change our ABI.
        shutil.copy2(self.binary, binary)
        symbol_file = root / "symbols.json"
        shutil.copy2(self.symbols, symbol_file)
        expected = json.loads(symbol_file.read_text())["binary_sha256"]
        if self.clean_input:
            from .input_clean import SUPPORTED_SHA256
            if expected != SUPPORTED_SHA256:
                raise WireError("Input cleanup requires the validated CLI binary")
        if hashlib.sha256(Path(binary).read_bytes()).hexdigest() != expected:
            raise WireError("CLI binary changed; regenerate and revalidate symbols")
        env = dict(environment, AGY_WIRE_SYMBOLS=str(symbol_file), AGY_WIRE_BINARY=binary)
        env['AGY_WIRE_INPUT_CLEAN'] = '1' if self.clean_input else '0'
        for name in ("stdin", "stdout", "events"):
            path = root / name
            os.mkfifo(path, 0o600)
            # The parent's RDWR handle keeps reader startup independent of GDB.
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            self.descriptors.append(fd)
            env["AGY_WIRE_" + name.upper()] = str(path)
            if name == "stdin":
                self.input_fd = fd
            else:
                reader = asyncio.StreamReader(limit=16*1024*1024)
                transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                    lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(os.dup(fd), "rb", buffering=0))
                self.transports.append(transport)
                self.tasks.append(asyncio.create_task(self._read(name, reader)))
        env["AGY_WIRE_STDERR"] = str(root / "cli.stderr")
        cli_args = self.cli_args if self.cli_args is not None else ["--model", self.model, "--effort", "low",
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--print-timeout", str(self.timeout)+"s", *self.extra]
        args = ["gdb", "-q", "-batch", "-x", str(Path(__file__).with_name("probe_gdb.py")),
                "--args", binary, *cli_args]
        self.log = (root / "debugger.log").open("wb")
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=self.log, stderr=self.log, start_new_session=True)
        self.tasks.append(asyncio.create_task(self._watch()))
        try:
            async with asyncio.timeout(15):
                while True:
                    source, event = await self.queue.get()
                    if source == "events" and event.get("kind") == "inferior_started":
                        self.inferior_pid = event["pid"]
                    if source == "events" and event.get("kind") == "probe_ready":
                        self.inferior_pid = event["pid"]
                        return self
                    self._check_failure(source, event)
        except BaseException:
            await self.close()
            raise

    async def _read(self, name, reader):
        try:
            while line := await reader.readline():
                event = json.loads(line)
                event["observed_at"] = time.monotonic()
                await self.queue.put((name, event))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.queue.put(("failure", {"error": type(exc).__name__}))

    async def _watch(self):
        code = await self.proc.wait()
        await asyncio.sleep(.05)
        await self.queue.put(("process_exit", {"code": code}))

    @staticmethod
    def _check_failure(source, event):
        if event.get('kind') == 'short_write':
            import logging
            logging.getLogger('agy_proxy.native_chat').info('TLS write observed requested=%s written=%s',
                event.get('requested'), event.get('written'))
        if event.get('kind') == 'framing_error':
            import logging
            logging.getLogger('agy_proxy.native_chat').warning('capture framing diagnostic %s', event)
        if source in ("failure", "process_exit"):
            raise WireError(f"CLI observation ended unexpectedly: {source}")
        if event.get("kind") in ("hook_error", "orphan_data", "sse_invalid",
                                 "request_parse_error", "response_parse_error"):
            import logging
            logging.getLogger('agy_proxy.native_chat').warning('capture diagnostic kind=%s stage=%s error=%s frames=%s',
                event['kind'], event.get('stage'), event.get('error'), event.get('frames'))
            raise WireError(f"Incomplete native capture: {event['kind']}")

    async def _write(self, value):
        data = memoryview(value)
        while data:
            try:
                n = os.write(self.input_fd, data)
                data = data[n:]
            except BlockingIOError:
                await asyncio.sleep(.001)

    async def turn(self, prompt):
        """Yield live native content, then a CLI-confirmed terminal result.

        A partially delivered turn is never retried. Closing/cancelling the
        generator destroys this experimental session and its CLI process.
        """
        async with self.lock:
            if self.closed or self.proc is None or self.proc.returncode is not None:
                raise WireError("Native session is not running")
            self.turns += 1
            number = self.turns
            started = time.monotonic()
            selected = {}
            completed = set()
            result = None
            first_wire = first_stdout = first_call = cli_finished = None
            text = []
            calls = []
            wire_usage = {}
            finish_reasons = []
            complete = False
            try:
                deadline = started + self.timeout
                async with asyncio.timeout_at(deadline):
                    await self._write((json.dumps({"event":"user","message":{"content":prompt}})+"\n").encode())
                while True:
                    async with asyncio.timeout_at(deadline):
                        source, event = await self.queue.get()
                    self._check_failure(source, event)
                    if source == "stdout":
                        kind = event.get("event")
                        if kind == "init":
                            if not event.get("conversation_id") or (self.conversation_id and self.conversation_id != event["conversation_id"]):
                                raise WireError("CLI conversation changed")
                            self.conversation_id = event.get("conversation_id")
                            if self.yield_cli_steps:
                                yield {"kind": "cli_init", "init": event.get("init"), "conversation_id": self.conversation_id}
                        elif kind == "step_update":
                            step = event.get("step_update") or {}
                            if self.yield_cli_steps:
                                yield {"kind": "cli_step", "step": step}
                            if step.get("text_delta") and first_stdout is None:
                                first_stdout = event["observed_at"]
                        elif kind == "result":
                            result = event["result"]
                            cli_finished = event["observed_at"]
                            if result.get("status") != "SUCCESS":
                                raise WireError("CLI did not confirm a successful turn")
                            if result.get("num_turns") != number:
                                raise WireError("CLI turn number mismatch")
                            if result.get("conversation_id") != self.conversation_id:
                                raise WireError("CLI conversation changed")
                    elif source == "events":
                        # The two FIFOs are independently scheduled. Timestamp
                        # filtering excludes delayed observations of older turns.
                        if event.get("monotonic",0) < started:
                            continue
                        kind, request_id = event.get("kind"), event.get("request_id")
                        if kind == "generation_request" and event.get("request_type") == "agent":
                            if not request_id:
                                raise WireError("Native request has no correlation id")
                            selected[request_id] = event
                        elif request_id in selected:
                            if kind in ("sse", "json_response"):
                                if event.get("usage"):
                                    wire_usage[request_id] = event["usage"]
                                for candidate in event["candidates"]:
                                    if candidate.get("index",0) != 0:
                                        raise WireError("Multiple candidates are not supported by this prototype")
                                    if candidate.get("finishReason"):
                                        finish_reasons.append(candidate["finishReason"])
                                    for part in candidate["parts"]:
                                        if part.get("thought"):
                                            continue
                                        if part.get("text"):
                                            first_wire = first_wire or event["monotonic"]
                                            text.append(part["text"])
                                            yield {"kind":"text", "text":part["text"], "request_id":request_id,
                                                   "turn":number, "wire_at":event["monotonic"],
                                                   "delivered_at":time.monotonic()}
                                        if part.get("functionCall"):
                                            first_call = first_call or event["monotonic"]
                                            calls.append(part["functionCall"])
                                            yield {"kind":"function_call", "call":part["functionCall"],
                                                   "request_id":request_id, "turn":number,
                                                   "wire_at":event["monotonic"]}
                            elif kind == "message_complete" and event.get("direction") == "recv":
                                completed.add(request_id)
                    if result is not None and selected and selected.keys() <= completed:
                        break
                cumulative = result.get("usage") or {}
                usage = {key:value-self._usage_totals.get(key,0) for key,value in cumulative.items()}
                if any(value < 0 for value in usage.values()):
                    raise WireError("CLI cumulative usage went backwards")
                self._usage_totals = cumulative
                self.last_metrics = {
                    "turn":number,"agent_requests":len(selected),
                    "first_wire_ms":None if first_wire is None else (first_wire-started)*1000,
                    "first_function_call_ms":None if first_call is None else (first_call-started)*1000,
                    "first_stdout_delta_ms":None if first_stdout is None else (first_stdout-started)*1000,
                    "cli_result_ms":(cli_finished-started)*1000,
                    "wire_lead_over_result_ms":None if first_wire is None else (cli_finished-first_wire)*1000,
                    "finish_reasons":finish_reasons,
                    "function_calling_modes":[e.get('function_calling_mode') for e in selected.values()],
                }
                if not finish_reasons:
                    raise WireError("Native response has no finish reason")
                complete = True
                yield {"kind":"result","cli":result,"text":"".join(text),"calls":calls,
                       "wire_usage":wire_usage,"usage":usage,"metrics":self.last_metrics}
            finally:
                if not complete:
                    await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        # GDB and inferior may use different process groups. The PID came from
        # this session's probe_ready event; never signal an unrelated process.
        children = set()
        if self.proc and self.proc.returncode is None:
            # Also cover cancellation before probe_ready reaches our FIFO reader.
            todo = [self.proc.pid]
            while todo:
                parent = todo.pop()
                with contextlib.suppress(FileNotFoundError, ProcessLookupError):
                    for task in Path(f"/proc/{parent}/task").iterdir():
                        with contextlib.suppress(FileNotFoundError):
                            for child in (task / "children").read_text().split():
                                pid = int(child)
                                if pid not in children:
                                    children.add(pid)
                                    todo.append(pid)
        if self.inferior_pid:
            children.add(self.inferior_pid)
        for pid in children:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            await self.proc.wait()
        for pid in children:
            with contextlib.suppress(ChildProcessError):
                await asyncio.to_thread(os.waitpid, pid, 0)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for transport in self.transports:
            transport.close()
        for fd in self.descriptors:
            os.close(fd)
        if hasattr(self,"log"):
            self.log.close()
        if self.directory:
            self.directory.cleanup()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *args):
        await self.close()
