"""Opt-in Linux native observer backend for Chat Completions only.

Client tools still execute in the client. Raw finish content is independently
validated and must agree with CLI lifecycle confirmation before final delivery.
"""
from contextlib import aclosing
from dataclasses import replace
import logging
import sys
from pathlib import Path
import shutil

from .agy import AgyError, AgySession, SessionManager, _child_env

log = logging.getLogger("agy_proxy.native_chat")


def captured_output(result):
    calls = result.get("calls") or []
    if len(calls) != 1 or calls[0].get("name") != "finish":
        raise AgyError("Native capture requires exactly one finish submission", code="native_capture_invalid")
    args = calls[0].get("args")
    if not isinstance(args, dict):
        raise AgyError("Native finish arguments are invalid", code="native_capture_invalid")
    output = {key: value for key, value in args.items() if key not in {"toolAction", "toolSummary"}}
    if output != result["cli"].get("structured_output"):
        raise AgyError("Native capture differs from CLI-confirmed output", code="native_capture_mismatch")
    return output


class NativeAgySession(AgySession):
    observer = None

    async def start(self):
        if sys.platform != "linux" or not shutil.which("gdb"):
            raise AgyError("Native Chat backend requires Linux and GDB", status_code=503)
        if self.attachments:
            raise AgyError("Native Chat image support is not yet verified", status_code=400)
        if self.output_schema is None:
            raise AgyError("Native Chat requires a restricted output contract", status_code=400)
        from tools.agy_native_wire_probe.runtime import NativeSession, WireError
        argv, cwd = self._launch_spec()
        binary = shutil.which(argv[0]) or argv[0]
        self.observer = NativeSession(binary=str(Path(binary).resolve()), cli_args=argv[1:],
                                      cwd=cwd, symbols=self.cfg.native_symbols,
                                      timeout=self.cfg.turn_timeout, environment=_child_env(self.cfg),
                                      yield_cli_steps=True, clean_input=self.cfg.native_input_clean)
        try:
            await self.observer.start()
        except (WireError, OSError) as exc:
            raise AgyError("Native Chat observer failed to start: " + str(exc), status_code=503) from exc
        self.proc = self.observer.proc

    async def send_turn(self, prompt):
        from tools.agy_native_wire_probe.runtime import WireError
        async with self.lock:
            try:
                async with aclosing(self.observer.turn(prompt)) as events:
                    async for event in events:
                        if event["kind"] == "cli_init":
                            payload = event.get("init")
                            if self.init_payload is not None or not isinstance(payload, dict) or payload.get("agent") != "proxy-external":
                                raise AgyError("Native CLI did not select the restricted agent", code="native_agent_invalid")
                            self.init_payload = payload
                            yield {"event": "init", "init": payload, "conversation_id": event['conversation_id']}
                        elif event["kind"] == "cli_step":
                            yield {"event": "step_update", "step_update": event["step"]}
                        elif event["kind"] == "result":
                            if self.init_payload is None:
                                raise AgyError("Native CLI did not confirm initialization", code="native_agent_invalid")
                            output = captured_output(event)
                            self.turns = self.observer.turns
                            self.conversation_id = self.observer.conversation_id
                            envelope = dict(event["cli"], structured_output=output, usage=event["usage"])
                            log.info("native capture verified conversation=%s turn=%d agent_requests=%d",
                                     self.conversation_id, self.turns, event["metrics"]["agent_requests"])
                            yield {"event": "result", "result": envelope}
            except WireError as exc:
                raise AgyError(str(exc), code="native_capture_failed") from exc

    async def kill(self):
        self._dead = True
        if self.observer is not None:
            await self.observer.close()
        if self._workspace is not None:
            self._workspace.cleanup()
            self._workspace = None

    async def close(self):
        await self.kill()


class NativeSessionManager(SessionManager):
    session_type = NativeAgySession

    def __init__(self, cfg):
        # No automatic regeneration/retry while validating captured native data.
        super().__init__(replace(cfg, agy_bin=cfg.native_bin or cfg.agy_bin,
                                 recovery_attempts=0, prewarm_models=()))
