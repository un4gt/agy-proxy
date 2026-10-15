"""ASGI correlation and wall timing without buffering or inspecting payloads."""
import asyncio
import logging
import uuid

from . import json_codec as json
from .latency import LatencyTrace

log = logging.getLogger("agy_proxy.latency")


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = "agy_" + uuid.uuid4().hex
        path = scope.get("path", "")
        protocol = ("chat" if path == "/v1/chat/completions" else "responses" if path.startswith("/v1/responses")
                    else "messages" if path.startswith("/v1/messages")
                    else "gemini" if path.startswith(("/v1/", "/v1beta/")) and path.endswith((":generateContent", ":streamGenerateContent"))
                    else "other")
        state = scope.setdefault("state", {})
        state.update(agy_request_id=request_id, agy_protocol=protocol)
        # Do not log health checks, dashboard assets or conversation retrieval.
        trace = LatencyTrace() if scope.get("method") == "POST" and (
            path in ("/v1/chat/completions", "/v1/responses", "/v1/messages") or protocol == "gemini") else None
        if trace is not None:
            state["latency_trace"] = trace
        status = None
        disconnected = False
        send_failed = False
        raised = None

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                if trace:
                    trace.mark("disconnect")
            return message

        async def correlated(message):
            nonlocal status, send_failed
            if message["type"] == "http.response.start":
                status = message["status"]
                message = dict(message, headers=list(message.get("headers", [])) +
                               [(b"x-agy-request-id", request_id.encode())])
            is_body = message["type"] == "http.response.body"
            business = bool(state.get("business_delivered")) or (
                not state.get("start_event_sent") and trace is not None and "validated_result" in trace.marks and status == 200)
            try:
                await send(message)
            except BaseException:
                send_failed = True
                raise
            # These are completed ASGI sends, not client acknowledgement/TTFT.
            if message["type"] == "http.response.start":
                state["headers_committed"] = True
                if trace:
                    trace.mark("headers_sent")
            elif trace and is_body:
                if message.get("body"):
                    trace.mark("first_body_sent")
                    if state.get("start_event_sent"):
                        trace.mark("start_event_sent")
                    if business:
                        trace.mark("first_business_sent")
                if not message.get("more_body"):
                    trace.mark("response_body_complete")
        try:
            await self.app(scope, observed_receive, correlated)
        except BaseException as exc:
            raised = "cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "error"
            raise
        finally:
            if trace:
                profile = state.get("turn_profile")
                error_code = profile.error_code if profile else None
                outcome = (raised or ("send_failed" if send_failed else None)
                           or ("cancelled" if disconnected and "response_body_complete" not in trace.marks else None)
                           or ("error" if error_code or status is None or status >= 400 else "success"))
                record = {"event": "request_latency", "version": 1, "request_id": request_id,
                          "protocol": protocol, "http_status": status, "outcome": outcome,
                          "attempts": profile.attempts if profile else 0,
                          "repair_attempts": profile.repair_attempts if profile else 0,
                          "error_code": error_code, **trace.public()}
                log.info("request_latency %s", json.dumps_text(record))
