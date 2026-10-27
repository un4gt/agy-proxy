"""One restricted CLI execution and recovery budget for every wire protocol."""
from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing

from anyio import CancelScope

from . import json_codec as json
from .account_pool import AccountPoolError
from .agy import AgyError, AgyQuotaError
from .contracts import CandidateError, CanonicalRequest
from .session_context import SessionContext

log = logging.getLogger("agy_proxy.execution")


async def execute(request: CanonicalRequest, mgr, *, profile, session_key=None):
    profile.model = request.model
    trace = profile.trace
    profile.mark("turn_start")
    context = SessionContext.build(session_key, request.configuration, request.messages)
    session = None
    result = None
    complete = False
    recovered = False
    repair = ""
    budget = min(1, max(0, mgr.cfg.recovery_attempts))
    deadline = profile.started + mgr.cfg.turn_timeout
    try:
        async with mgr.request_lease(context, timing=trace):
            try:
                # timeout_at cannot surround generator yields: consumers may pull
                # the next event in a different task (HTTP heartbeat handling).
                async def acquire():
                    async with asyncio.timeout_at(deadline):
                        return await mgr.acquire(model=request.model, effort=request.effort,
                                                 output_schema=request.output_schema,
                                                 session_instructions=request.session_instructions,
                                                 attachments=request.attachments, context=context, timing=trace)
                with trace.span("session_acquire"):
                    session = await acquire()
                profile.new_session = session.turns == 0
                profile.mark("session_ready")
                yield "ready", None
                while True:
                    candidate = None
                    usage_seen = False
                    usage_snapshot = None
                    profile.attempts += 1
                    try:
                        with trace.span("attempt", attempt=profile.attempts,
                                        pid=session.proc.pid if session.proc else None,
                                        new_session=session.turns == 0):
                            async with asyncio.timeout_at(deadline):
                                with trace.span("prompt_build", attempt=profile.attempts) as prompt_span:
                                    prompt = request.prompt(session) + repair
                                    prompt_span.args.update(prompt_chars=len(prompt),
                                                            session_instructions_chars=len(request.session_instructions))
                                with trace.span("cli_turn", attempt=profile.attempts) as cli_span:
                                    async with aclosing(session.send_turn(prompt)) as events:
                                        async for event in events:
                                            kind = event.get("event")
                                            if kind == "init":
                                                profile.mark("init_seen")
                                                trace.mark(f"attempt_{profile.attempts}_init")
                                            elif kind == "step_update":
                                                trace.mark(f"attempt_{profile.attempts}_first_step")
                                                session.check_tool_step(event.get("step_update") or {})
                                            elif kind == "usage":
                                                # Native capture reports an attempt-wide snapshot,
                                                # not a delta for each SSE chunk.
                                                usage_snapshot = event["usage"]
                                            elif kind == "result":
                                                cli_span.end()
                                                trace.mark(f"attempt_{profile.attempts}_cli_result")
                                                profile.add_usage(event["result"].get("usage"))
                                                profile.usage_complete &= event["result"].get("usage_complete", True)
                                                usage_seen = True
                                                with trace.span("validate", attempt=profile.attempts):
                                                    session.check_images_seen()
                                                    candidate = event["result"].get("structured_output")
                                                    if candidate is None:
                                                        candidate = event["result"].get("response")
                                                    result = request.validate(event["result"])
                                                    if request.clean_result:
                                                        request.clean_result(result, session)
                                if result is None:
                                    raise AgyError("CLI ended without a result")
                        break
                    except AgyError as exc:
                        if not usage_seen:
                            profile.add_usage(exc.usage or usage_snapshot)
                            profile.usage_complete &= getattr(exc, "usage_complete", usage_snapshot is None)
                            usage_seen = True
                        quota = isinstance(exc, AgyQuotaError)
                        if quota and mgr.cfg.account_pool:
                            try:
                                mgr.cfg.account_pool.mark_exhausted(exc.account_id, exc.model)
                            except AccountPoolError:
                                raise AgyError("Cannot update account quota state", status_code=503) from None
                        eligible = (isinstance(exc, CandidateError) or exc.retryable or
                                    (quota and exc.retry_safe and mgr.cfg.account_pool is not None))
                        feedback = exc.feedback if isinstance(exc, CandidateError) else {}
                        log.warning("candidate protocol=%s request_id=%s attempt=%s code=%s feedback=%s schemas=%s recover=%s",
                                    profile.protocol, profile.request_id, profile.attempts, exc.code,
                                    json.dumps_text(feedback),
                                    [(c.client_name, c.fingerprint) for c in request.tools.values()],
                                    bool(eligible and budget))
                        if not eligible or not budget:
                            raise
                        budget -= 1
                        recovered = True
                        if isinstance(exc, CandidateError):
                            profile.repair_attempts += 1
                            repair = ("\n\n[Rejected candidate; not client history]\n" + json.dumps_text(candidate)
                                      + "\n[Validation feedback]\n" + json.dumps_text(feedback)
                                      + "\nRegenerate the COMPLETE response using the original client contracts. "
                                      "Do not execute tools or invent missing facts. The rejected candidate was never delivered.")
                        with trace.span("recovery_restart", after_attempt=profile.attempts, error_code=exc.code):
                            async with asyncio.timeout_at(deadline):
                                try:
                                    session = await mgr.restart(session, reselect=quota, timing=trace)
                                except AgyError as restart_error:
                                    if quota and restart_error.status_code == 429:
                                        # No alternate account is available. Keep
                                        # the upstream quota reason and reset time.
                                        raise exc from restart_error
                                    raise
                    finally:
                        if not usage_seen and usage_snapshot is not None:
                            # Timeout and cancellation bypass the AgyError handler.
                            profile.add_usage(usage_snapshot)
                            profile.usage_complete = False
                result.usage = dict(profile.usage)
                profile.mark("result")
                trace.mark("validated_result")
                if result.text:
                    profile.mark("first_delta")
                    yield "delta", result.text
                complete = True
                yield "result", result
            finally:
                if session is not None:
                    with CancelScope(shield=True):
                        # Repair prompts and rejected candidates must never be
                        # retained in the next client's conversation.
                        with trace.span("session_release"):
                            await mgr.release(session, reply=request.confirmed_reply(result)
                                              if complete and not recovered else None)
    except TimeoutError:
        profile.error_code = "upstream_timeout"
        profile.usage_complete = False
        raise AgyError("Request exceeded the total CLI time budget", status_code=504) from None
    except AgyError as exc:
        profile.error_code = exc.code
        raise
    except BaseException:
        if not complete:
            profile.error_code = "cancelled"
            profile.usage_complete = False
        raise
    finally:
        log.info("execution protocol=%s request_id=%s attempts=%s repair_attempts=%s outcome=%s error_code=%s usage_complete=%s",
                 profile.protocol, profile.request_id, profile.attempts, profile.repair_attempts,
                 "success" if complete else "failed", profile.error_code, profile.usage_complete)
