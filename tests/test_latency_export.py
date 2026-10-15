"""Keep log framing, relative clocks and CLI PID metadata usable in Perfetto."""
import json

import pytest

from tools.export_latency import logs_trace


@pytest.mark.parametrize("framing", ["plain", "docker", "jsonl"])
def test_export_realistic_attempt_records(tmp_path, framing):
    row = {"version": 1, "event": "request_latency", "request_id": "agy_test", "protocol": "chat",
           "outcome": "success", "http_status": 200, "attempts": 1, "repair_attempts": 0,
           "error_code": None, "started_unix_us": 100000000, "total_ms": 250,
           "spans": [{"name": "attempt", "start_ms": 10, "duration_ms": 200, "outcome": "ok",
                      "attempt": 1, "pid": 456, "new_session": True}],
           "marks_ms": {"headers_sent": 3, "validated_result": 210, "first_business_sent": 220}}
    encoded = json.dumps(row)
    plain = "2026-10-14 INFO agy_proxy.latency: request_latency " + encoded
    line = {"plain": plain, "docker": json.dumps({"log": plain + "\n"}), "jsonl": encoded}[framing]
    path = tmp_path / "proxy.log"
    path.write_text(line + "\n", encoding="utf-8")
    trace = logs_trace(path)
    attempt = next(e for e in trace.events if e["name"] == "attempt")
    assert attempt["pid"] == 1 and attempt["args"]["cli_pid"] == 456
    assert attempt["ts"] == 10000 and attempt["dur"] == 200000
    assert next(e for e in trace.events if e["name"] == "first_business_sent")["ts"] == 220000
    with pytest.raises(ValueError, match="No matching"):
        logs_trace(path, "agy_missing")
    target = tmp_path / "trace.json"
    trace.write(target)
    assert json.loads(target.read_text(encoding="utf-8"))["traceEvents"] == trace.events
