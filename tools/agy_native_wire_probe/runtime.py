"""Experimental Linux native-CLI observer, with a live FIFO event channel.

The CLI owns all network traffic. stdout confirms lifecycle; TLS observations
provide content and token usage. This module is not enabled by the application's default path.
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
from collections import Counter

from tools.agy_cli_protocol import CliTurnCounter
from .usage import NativeUsage


CAPTURE_DRAIN_TIMEOUT = 5


class WireError(RuntimeError):
    def __init__(self, message, *, code='native_capture_failed'):
        super().__init__(message)
        self.code = code
        self.diagnostics = {}


class WireUpstreamError(WireError):
    def __init__(self, event):
        self.status_code = event['status']
        self.quota_exhausted = self.status_code == 429 and event.get('quota_exhausted') is True
        reason = ('quota exhausted' if self.quota_exhausted else
                  'rate limit exceeded' if self.status_code == 429 else 'request failed')
        message = f"Native upstream {reason} (HTTP {self.status_code})"
        if self.quota_exhausted and event.get('quota_reset_at'):
            message += f"; quota resets at {event['quota_reset_at']}"
        super().__init__(message, code='native_upstream_error')


class NativeSession:
    def __init__(self, *, model="gemini-3.8-flash-low", cwd="/workspace-empty",
                 extra=(), symbols="/evidence/symbols.json", timeout=120,
                 binary="/root/.local/bin/agy", cli_args=None, environment=None, yield_cli_steps=False,
                 clean_input=False, guard_finish=False, image_paths=()):
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
        self._cli_turns = CliTurnCounter()
        self.conversation_id = None
        self.closed = False
        self.last_metrics = None
        self.workspace = None
        self.binary = binary
        self.cli_args = cli_args
        self.environment = environment
        self.yield_cli_steps = yield_cli_steps
        self.clean_input = clean_input
        self.guard_finish = guard_finish
        self.image_paths = tuple(image_paths)

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
        symbols = json.loads(symbol_file.read_text())
        expected = symbols["binary_sha256"]
        if 'crypto/tls.(*Conn).Close' not in symbols.get('functions', {}):
            raise WireError("Native symbols lack the connection lifecycle hook; regenerate symbols")
        if self.clean_input:
            from .input_clean import SUPPORTED_SHA256
            from .input_guard import TRUNCATION_FUNCTION
            if expected != SUPPORTED_SHA256:
                raise WireError("Input cleanup requires the validated CLI binary")
            if TRUNCATION_FUNCTION not in symbols.get('functions', {}):
                raise WireError("Native symbols lack the input preservation hook; regenerate symbols")
        if hashlib.sha256(Path(binary).read_bytes()).hexdigest() != expected:
            raise WireError("CLI binary changed; regenerate and revalidate symbols")
        if self.guard_finish:
            from .finish_guard import validate_symbols
            try:
                validate_symbols(symbols)
            except ValueError as exc:
                raise WireError(str(exc), code='native_finish_guard_failed') from exc
        env = dict(environment, AGY_WIRE_SYMBOLS=str(symbol_file), AGY_WIRE_BINARY=binary)
        env['AGY_WIRE_INPUT_CLEAN'] = '1' if self.clean_input else '0'
        env['AGY_WIRE_FINISH_GUARD'] = '1' if self.guard_finish else '0'
        env['AGY_WIRE_IMAGE_PATHS'] = json.dumps(self.image_paths)
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
            logging.getLogger('agy_proxy.native_chat').warning('capture diagnostic kind=%s stage=%s error=%s code=%s frames=%s',
                event['kind'], event.get('stage'), event.get('error'), event.get('code'), event.get('frames'))
            if event.get('kind') == 'hook_error' and event.get('code') in {
                'native_input_too_large', 'native_input_guard_failed',
                'native_input_cleanup_failed', 'native_input_user_metadata_invalid',
                'native_finish_guard_failed',
            }:
                raise WireError(f"Native guard failed: {event['code']}", code=event['code'])
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
            self._cli_turns.begin()
            number = self.turns
            started = time.monotonic()
            selected = {}
            completed = set()
            result = None
            first_wire = first_stdout = first_call = cli_finished = None
            text = []
            calls = []
            wire_usage = {}
            accounting = NativeUsage()
            finish_reasons = []
            finish_exhausted = False
            finish_output_turns = None
            observations = Counter()
            complete = False
            try:
                deadline = started + self.timeout
                capture_deadline = deadline
                async with asyncio.timeout_at(deadline):
                    await self._write((json.dumps({"event":"user","message":{"content":prompt}})+"\n").encode())
                while True:
                    try:
                        async with asyncio.timeout_at(capture_deadline):
                            source, event = await self.queue.get()
                    except TimeoutError as exc:
                        if result is not None:
                            if (selected and selected.keys() <= completed and
                                    len(selected) >= self._cli_turns.input_count and
                                    not any(call.get('name') == 'finish' for call in calls) and
                                    (not self.guard_finish or finish_output_turns == result.get('num_turns'))):
                                # A complete text candidate is repairable even when
                                # the CLI retains a non-null structured_output.
                                break
                            raise WireError(
                                "Incomplete native capture after CLI result "
                                f"(inputs={self._cli_turns.input_count}, requests={len(selected)}, "
                                f"completed={len(completed)}, calls={len(calls)})",
                                code="native_capture_incomplete") from exc
                        raise
                    if source == 'events':
                        kind = event.get('kind', 'unknown')
                        if kind in {'tls_io', 'request', 'response', 'generation_request', 'sse',
                                    'json_response', 'message_complete', 'connection_protocol',
                                    'connection_closed', 'input_cleaned', 'upstream_error', 'finish_boundary',
                                    'finish_exhausted', 'finish_output_checked'}:
                            observations[kind] += 1
                        if kind == 'connection_protocol' and event.get('reused_address'):
                            observations['reused_address'] += 1
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
                            if step.get("conversation_id") not in (None, self.conversation_id):
                                raise WireError("CLI step belongs to another conversation")
                            try:
                                self._cli_turns.observe(step)
                            except ValueError as exc:
                                raise WireError(str(exc), code="native_turn_invalid") from exc
                            if self.yield_cli_steps:
                                yield {"kind": "cli_step", "step": step}
                            if step.get("text_delta") and first_stdout is None:
                                first_stdout = event["observed_at"]
                        elif kind == "result":
                            result = event["result"]
                            cli_finished = event["observed_at"]
                            # All upstream work is done. Only FIFO delivery can
                            # remain; do not spend the model timeout on a lost capture.
                            capture_deadline = min(deadline, time.monotonic() + CAPTURE_DRAIN_TIMEOUT)
                            if result.get("status") != "SUCCESS":
                                raise WireError("CLI did not confirm a successful turn")
                            if result.get("conversation_id") != self.conversation_id:
                                raise WireError("CLI conversation changed")
                            try:
                                self._cli_turns.confirm(result.get("num_turns"))
                            except ValueError as exc:
                                raise WireError(str(exc), code="native_turn_invalid") from exc
                    elif source == "events":
                        # The two FIFOs are independently scheduled. Timestamp
                        # filtering excludes delayed observations of older turns.
                        if event.get("monotonic",0) < started:
                            observations['stale_events'] += 1
                            continue
                        kind, request_id = event.get("kind"), event.get("request_id")
                        if kind == 'finish_exhausted':
                            finish_exhausted = True
                        elif kind == 'finish_output_checked':
                            finish_output_turns = event.get('cli_turns')
                        if kind == "generation_request" and event.get("request_type") == "agent":
                            if not request_id:
                                raise WireError("Native request has no correlation id")
                            selected[request_id] = event
                        elif request_id in selected:
                            if kind == 'upstream_error':
                                raise WireUpstreamError(event)
                            if kind in ("sse", "json_response"):
                                accounting.observe(request_id, event.get("usage"), finished=any(
                                    candidate.get("finishReason") for candidate in event["candidates"]))
                                if event.get("usage") is not None:
                                    wire_usage[request_id] = event["usage"]
                                    # Keep known consumption even if capture, validation,
                                    # cancellation or the CLI lifecycle fails afterwards.
                                    yield {"kind": "usage", "usage": accounting.snapshot(selected, completed)[0]}
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
                        elif kind in ('sse', 'json_response'):
                            observations['unmatched_responses'] += 1
                    if result is not None and selected and selected.keys() <= completed:
                        # stdout can overtake the independent capture FIFO. A
                        # reminder requires another request, and a structured
                        # result requires its captured submission before validation.
                        if (not self.guard_finish or finish_output_turns == result.get('num_turns')) and len(selected) >= self._cli_turns.input_count and (
                            any(call.get('name') == 'finish' for call in calls) or
                            (result.get("structured_output") is None and not calls)
                        ):
                            break
                usage, usage_complete = accounting.snapshot(selected, completed)
                self.last_metrics = {
                    "turn":number,"agent_requests":len(selected),
                    "cli_turns": self._cli_turns.completed,
                    "first_wire_ms":None if first_wire is None else (first_wire-started)*1000,
                    "first_function_call_ms":None if first_call is None else (first_call-started)*1000,
                    "first_stdout_delta_ms":None if first_stdout is None else (first_stdout-started)*1000,
                    "cli_result_ms":(cli_finished-started)*1000,
                    "wire_lead_over_result_ms":None if first_wire is None else (cli_finished-first_wire)*1000,
                    "finish_reasons":finish_reasons,
                    "function_calling_modes":[e.get('function_calling_mode') for e in selected.values()],
                    "finish_exhausted":finish_exhausted,
                }
                if not finish_reasons:
                    raise WireError("Native response has no finish reason")
                complete = True
                yield {"kind":"result","cli":result,"text":"".join(text),"calls":calls,
                       "wire_usage":wire_usage,"usage":usage,"usage_complete":usage_complete,
                       "metrics":self.last_metrics}
            except WireError as exc:
                exc.diagnostics = dict(observations)
                raise
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
