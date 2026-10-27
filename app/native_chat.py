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

from .agy import AgyError, AgyQuotaError, AgySession, SessionManager, _child_env
from .contracts import CandidateError
from .tool_guard import allowed_image_call

log = logging.getLogger("agy_proxy.native_chat")


def captured_output(result, *, image_paths=(), images_seen=()):
    if result.get('metrics', {}).get('finish_exhausted'):
        raise AgyError("Native executor exhausted current-turn finish submissions",
                       code="native_finish_exhausted")
    calls = result.get("calls") or []
    submissions = []
    for call in calls:
        if not isinstance(call, dict):
            raise AgyError("Native function call is invalid", code="native_capture_invalid")
        if call.get("name") == "finish":
            submissions.append(call)
        elif not allowed_image_call(call, image_paths):
            raise AgyError("Native capture contains an unapproved server tool call",
                           code="native_tool_violation")
        elif call["args"]["AbsolutePath"] not in images_seen:
            raise AgyError("Native attachment read was not confirmed successful",
                           code="native_image_incomplete")
    if set(image_paths) != set(images_seen):
        raise AgyError("Native response did not inspect every supplied image",
                       code="native_image_incomplete")
    calls = submissions
    if not calls:
        # A confirmed text-only turn is a rejected candidate, not an observer
        # failure. Nothing has been delivered/executed on the client yet.
        raise CandidateError("Native response omitted its finish submission",
                             code="native_finish_missing", feedback={
                                 "required_submission": "finish",
                                 "instruction": "Invoke finish exactly once with the complete structured client response; plain text is not a submission."})
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
        if self.output_schema is None:
            raise AgyError("Native Chat requires a restricted output contract", status_code=400)
        from tools.agy_native_wire_probe.runtime import NativeSession, WireError
        argv, cwd = self._launch_spec()
        binary = shutil.which(argv[0]) or argv[0]
        self.observer = NativeSession(binary=str(Path(binary).resolve()), cli_args=argv[1:],
                                      cwd=cwd, symbols=self.cfg.native_symbols,
                                      timeout=self.cfg.turn_timeout, environment=_child_env(self.cfg),
                                      yield_cli_steps=True, clean_input=self.cfg.native_input_clean,
                                      guard_finish=True, image_paths=self.image_paths)
        try:
            await self.observer.start()
        except (WireError, OSError) as exc:
            raise AgyError("Native Chat observer failed to start: " + str(exc), status_code=503) from exc
        self.proc = self.observer.proc

    async def send_turn(self, prompt):
        from tools.agy_native_wire_probe.runtime import WireError, WireUpstreamError
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
                            self.check_tool_step(event["step"])
                            yield {"event": "step_update", "step_update": event["step"]}
                        elif event["kind"] == "usage":
                            yield {"event": "usage", "usage": event["usage"]}
                        elif event["kind"] == "result":
                            if self.init_payload is None:
                                raise AgyError("Native CLI did not confirm initialization", code="native_agent_invalid")
                            try:
                                output = captured_output(event, image_paths=self.image_paths,
                                                         images_seen=self.images_seen)
                            except AgyError as exc:
                                exc.usage = event["usage"]
                                exc.usage_complete = event.get("usage_complete", False)
                                log.warning("native capture rejected conversation=%s turn=%d code=%s calls=%d agent_requests=%s finish_reasons=%s calling_modes=%s",
                                            self.observer.conversation_id, self.observer.turns, exc.code,
                                            len(event.get("calls") or []), event["metrics"].get("agent_requests"),
                                            event["metrics"].get("finish_reasons"), event["metrics"].get("function_calling_modes"))
                                raise
                            self.turns = self.observer.turns
                            self.conversation_id = self.observer.conversation_id
                            envelope = dict(event["cli"], structured_output=output, usage=event["usage"],
                                            usage_complete=event.get("usage_complete", False))
                            log.info("native capture verified conversation=%s turn=%d cli_turns=%s agent_requests=%d cli_result_ms=%s",
                                     self.conversation_id, self.turns, event["metrics"].get("cli_turns"),
                                     event["metrics"]["agent_requests"], event["metrics"].get("cli_result_ms"))
                            yield {"event": "result", "result": envelope}
            except WireError as exc:
                log.warning("native capture failed conversation=%s turn=%s code=%s observations=%s",
                            getattr(self.observer, 'conversation_id', self.conversation_id),
                            getattr(self.observer, 'turns', self.turns), exc.code, exc.diagnostics)
                if isinstance(exc, WireUpstreamError):
                    if exc.quota_exhausted and self.account_id:
                        # Native server tools are restricted to attachment reads;
                        # client tool calls have not been delivered or executed.
                        raise AgyQuotaError(str(exc), self.account_id, self.model, retry_safe=True) from exc
                    raise AgyError(str(exc), status_code=exc.status_code,
                                   code='quota_exhausted' if exc.quota_exhausted else
                                   'rate_limit_exceeded' if exc.status_code == 429 else exc.code) from exc
                raise AgyError(str(exc), code=exc.code,
                               status_code=413 if exc.code == 'native_input_too_large' else 502,
                               retryable=exc.code == 'native_capture_incomplete') from exc

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
        # One bounded, pre-delivery repair uses the shared execution deadline,
        # full client history and usage accounting. Capture mismatches stay fatal.
        super().__init__(replace(cfg, agy_bin=cfg.native_bin or cfg.agy_bin,
                                 recovery_attempts=1, prewarm_models=()))
