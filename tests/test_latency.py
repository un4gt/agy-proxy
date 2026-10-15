"""Trace semantics across real subprocess attempts and ASGI delivery failures."""
import asyncio
import json
import logging

import pytest

from app.request_context import RequestContextMiddleware
from app.runner import TurnProfile
from test_unified_execution import PROTOCOLS, client, wire


def records(caplog):
    return [json.loads(r.getMessage().split("request_latency ", 1)[1])
            for r in caplog.records if r.name == "agy_proxy.latency"]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
def test_correlated_trace_separates_heartbeat_repair_and_delivery(client, caplog, protocol, stream):
    caplog.set_level(logging.INFO, logger="agy_proxy.latency")
    secret = "PRIVATE_ARGUMENT_NEVER_LOG"
    url, body = wire(protocol, 'REPAIR_ARGS\n{"path":"' + secret + '"}', stream=stream)
    response = client.post(url, json=body)
    assert response.status_code == 200
    [row] = records(caplog)
    assert row["request_id"] == response.headers["x-agy-request-id"]
    assert row["protocol"] == protocol and row["outcome"] == "success"
    assert row["attempts"] == 2 and row["repair_attempts"] == 1
    assert secret not in json.dumps(row)
    marks = row["marks_ms"]
    assert marks["first_business_sent"] >= marks["validated_result"]
    assert marks["response_body_complete"] <= row["total_ms"]
    if stream:
        assert marks["start_event_sent"] < marks["validated_result"]
    spans = row["spans"]
    assert all(s["duration_ms"] is not None and s["duration_ms"] >= 0 for s in spans)
    assert [s["attempt"] for s in spans if s["name"] == "cli_turn"] == [1, 2]
    # Invalid arguments are a validation failure, not a CLI transport failure.
    assert [s["outcome"] for s in spans if s["name"] == "cli_turn"] == ["ok", "ok"]
    assert [s["outcome"] for s in spans if s["name"] == "validate"] == ["error", "ok"]
    restart = next(s for s in spans if s["name"] == "recovery_restart")
    assert restart["after_attempt"] == 1
    assert all(s["start_ms"] + s["duration_ms"] <= row["total_ms"] + .002 for s in spans)


@pytest.mark.parametrize("stream", [False, True])
def test_exhausted_repair_never_marks_business_as_delivered(client, caplog, stream):
    caplog.set_level(logging.INFO, logger="agy_proxy.latency")
    url, body = wire("chat", "BAD_ARGS", stream=stream)
    response = client.post(url, json=body)
    assert response.status_code == (200 if stream else 502)
    [row] = records(caplog)
    assert row["outcome"] == "error" and row["error_code"] == "tool_arguments_invalid"
    assert row["attempts"] == 2
    assert "first_business_sent" not in row["marks_ms"]
    assert "validated_result" not in row["marks_ms"]
    assert "response_body_complete" in row["marks_ms"]


def test_timeout_closes_attempt_span_without_delivery(client, caplog):
    caplog.set_level(logging.INFO, logger="agy_proxy.latency")
    client.app.state.cfg.turn_timeout = .4
    url, body = wire("chat", "SLOW_OUTPUT")
    assert client.post(url, json=body).status_code == 504
    [row] = records(caplog)
    assert row["error_code"] == "upstream_timeout"
    assert "first_business_sent" not in row["marks_ms"]
    assert all(s["duration_ms"] is not None for s in row["spans"])


@pytest.mark.parametrize("failure", ["headers", "body", "cancelled"])
def test_asgi_failure_is_logged_once_without_false_delivery(caplog, failure):
    caplog.set_level(logging.INFO, logger="agy_proxy.latency")

    async def scenario():
        async def app(scope, receive, send):
            trace = scope["state"]["latency_trace"]
            profile = TurnProfile(trace=trace)
            scope["state"]["turn_profile"] = profile
            if failure == "cancelled":
                with trace.span("cli_turn", attempt=1):
                    raise asyncio.CancelledError()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            trace.mark("validated_result")
            await send({"type": "http.response.body", "body": b"PRIVATE_RESULT"})

        async def send(message):
            if failure == "headers" or message["type"] == "http.response.body":
                raise OSError("PRIVATE_ERROR")

        async def receive():
            return {"type": "http.request", "body": b""}

        scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions"}
        with pytest.raises((OSError, asyncio.CancelledError)):
            await RequestContextMiddleware(app)(scope, receive, send)
    asyncio.run(scenario())
    [row] = records(caplog)
    assert row["outcome"] in ("error", "cancelled")
    assert "first_business_sent" not in row["marks_ms"]
    assert "response_body_complete" not in row["marks_ms"]
    assert "PRIVATE" not in json.dumps(row)
    if failure == "cancelled":
        assert row["spans"][0]["outcome"] == "cancelled"


def test_input_error_and_health_requests(client, caplog):
    caplog.set_level(logging.INFO, logger="agy_proxy.latency")
    client.get("/health")
    assert not records(caplog)
    assert client.post("/v1/chat/completions", json={}).status_code == 400
    [row] = records(caplog)
    assert row["attempts"] == 0 and row["outcome"] == "error"
