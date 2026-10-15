"""Opt-in live acceptance against an isolated instance, never the production port.

python tests/unified_cli_smoke.py --clients pi codex claude opencode
Uses existing AGY login; generated workspaces/configs stay under .preview.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
from pathlib import Path
import shutil
import socket
import sys
import threading
import time
import uuid

import httpx
from PIL import Image, ImageDraw, ImageFont
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402
import mcp_smoke as common  # noqa: E402
from test_unified_execution import wire, payload, calls_from, append_results  # noqa: E402


def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            return origin, server, thread
        time.sleep(0.05)
    raise RuntimeError("Isolated proxy startup failed")


def api_checks(origin, key, model, folder):
    rows = []
    with httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + key}, timeout=200, trust_env=False) as client:
        for protocol in ("chat", "responses", "messages", "gemini"):
            for stream in (False, True):
                args = {"command": "echo SYNTHETIC", "description": "synthetic contract check"}
                schema = {"type": "object", "properties": {k: {"type": "string"} for k in args},
                          "required": list(args), "additionalProperties": False}
                url, body = wire(protocol, "Call bash once using exactly these arguments: " + json.dumps(args),
                                 schema=schema, choice=True, stream=stream)
                if protocol == "gemini":
                    url = url.replace("models/auto", "models/" + model)
                else:
                    body["model"] = model
                started = time.monotonic()
                try:
                    response = client.post(url, json=body)
                    result = payload(response, protocol, stream)
                    calls = calls_from(protocol, result)
                    assert len(calls) == 1 and calls[0][2] == args, "arguments mismatch"
                    append_results(protocol, body, result, calls)
                    # The synthetic client reports the result; the proxy never
                    # executes the requested shell command.
                    if protocol == "gemini":
                        body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
                    elif protocol == "messages":
                        body["tool_choice"] = {"type": "none"}
                    else:
                        body["tool_choice"] = "none"
                    final = payload(client.post(url, json=body), protocol, stream)
                    assert not calls_from(protocol, final)
                    rows.append({"protocol": protocol, "stream": stream, "passed": True,
                                 "seconds": round(time.monotonic() - started, 1), "request_id": response.headers["x-agy-request-id"]})
                except Exception as exc:
                    rows.append({"protocol": protocol, "stream": stream, "passed": False, "reason": str(exc)[:400]})
                common.write_json(folder / "api-results.json", rows)
                print(json.dumps(rows[-1]), flush=True)
        # Image read allowlist, including the newly supported Gemini path.
        image = Image.new("RGB", (600, 180), "white")
        ImageDraw.Draw(image).text((30, 30), "AGY 7319", font=ImageFont.truetype("arial.ttf", 80), fill="black")
        buf = io.BytesIO()
        image.save(buf, "PNG")
        encoded = base64.b64encode(buf.getvalue()).decode()
        for protocol in ("chat", "responses", "messages", "gemini"):
            url, body = wire(protocol, "Read the text in the image. Reply with only the text.", tools=False)
            part = ({"inlineData": {"mimeType": "image/png", "data": encoded}} if protocol == "gemini" else
                    {"type": "input_image", "image_url": "data:image/png;base64," + encoded} if protocol == "responses" else
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}} if protocol == "messages" else
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}})
            if protocol == "gemini":
                body["contents"][0]["parts"].append(part)
                url = url.replace("models/auto", "models/" + model)
            else:
                body["model"] = model
                message = body["input" if protocol == "responses" else "messages"][0]
                message["content"] = [{"type": "input_text" if protocol == "responses" else "text", "text": message["content"]}, part]
            response = client.post(url, json=body)
            rows.append({"protocol": protocol, "image": True, "status": response.status_code,
                         "passed": response.status_code == 200 and "7319" in response.text})
            common.write_json(folder / "api-results.json", rows)
            print(json.dumps(rows[-1]), flush=True)
    return rows


def client_check(name, folder, origin, key, model):
    directory, work, env, command = common.setup_client(name, folder, origin, key)
    cfg = directory / "config"
    # Use the bundled filesystem MCP, with no remote/search credentials.
    if name in ("pi", "claude"):
        mcp = json.loads((cfg / "mcp.json").read_text())
        mcp["mcpServers"].pop("exa", None)
        common.write_json(cfg / "mcp.json", mcp)
    elif name == "codex":
        for i, value in enumerate(command):
            if value.startswith("mcp_servers="):
                fs = {"filesystem": {"command": str(common.NPX), "args": ["-y", "@modelcontextprotocol/server-filesystem", str(work)],
                      "startup_timeout_sec": 60, "tools": {t: {"approval_mode": "approve"} for t in
                      ("read_text_file", "read_multiple_files", "write_file", "edit_file")}}}
                command[i] = "mcp_servers=" + common.toml_value(fs)
        command += ["-c", "agents.enabled=false", "-c", "model_catalog_json=" + common.toml_value(str(ROOT / "examples/vision/codex-models.json"))]
    else:
        config = json.loads((cfg / "opencode.json").read_text())
        config["mcp"].pop("exa", None)
        common.write_json(cfg / "opencode.json", config)
    if name == "claude":
        command[command.index("--model") + 1] = model
        for variable in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                         "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            env[variable] = model
        command += ["--permission-mode", "default"]
    marker = name + "_" + uuid.uuid4().hex[:12]
    (work / "requirements.md").write_text(
        "Implement add(a,b) in sum.mjs, exporting add. Return a+b. Do not change sum.test.mjs. "
        f"This project marker is {marker}.\n", encoding="utf-8")
    (work / "sum.mjs").write_text("export const add = (a,b) => 0;\n")
    tests = "import assert from 'node:assert/strict'; import {add} from './sum.mjs'; assert.equal(add(2,3),5); assert.equal(add(-3,1),-2); console.log('SUM_OK');\n"
    (work / "sum.test.mjs").write_text(tests)
    test_command = "node --test --test-isolation=none sum.test.mjs" if name == "codex" else "node --test sum.test.mjs"
    prompt = (f"Work only in {work}. Use filesystem MCP to read requirements.md and sum.mjs and implement the requested change. "
              f"Run `{test_command}` using your shell tool and fix any failure. Do not change tests or start other agents. "
              "Report the test result and remember the project marker for our next turn.")
    rows = []
    for phase in (1, 2):
        args = list(command)
        if phase == 2:
            session = common.session_id(name, directory / "phase1.jsonl")
            if name == "codex":
                args += ["resume", session]
            elif name == "claude":
                args += ["--resume", session]
            elif name == "opencode":
                args += ["--session", session]
            prompt = (f"Continue this conversation: run `{test_command}` again, then report the project marker from our previous turn. "
                      "Do not read files to recover the marker or start other agents.")
        log = directory / f"phase{phase}.jsonl"
        code = common.run_process([*args, prompt], work, env, log, [key], timeout=300)
        import subprocess
        check = subprocess.run(["node", "sum.test.mjs"], cwd=work, capture_output=True, text=True)
        audit = common.audit_log(name, log, marker, phase)
        row = {"client": name, "phase": phase, "exit_code": code, "tests_pass": check.returncode == 0,
               "tests_unchanged": (work / "sum.test.mjs").read_text() == tests,
               "marker": audit["session_marker_present"], "tool_calls": audit["tool_calls"],
               "tool_errors": audit["tool_errors"], "client_ran_tests": audit["client_ran_tests"],
               "mcp_read_write": audit["filesystem_read"] and audit["filesystem_write"]}
        row["passed"] = (code == 0 and row["tests_pass"] and row["tests_unchanged"] and row["marker"]
                         and row["client_ran_tests"] and not row["tool_errors"]
                         and (phase == 2 or row["mcp_read_write"]))
        rows.append(row)
        common.write_json(directory / "result.json", rows)
        print(json.dumps(row), flush=True)
        if not row["passed"]:
            break
    return rows


def failure_checks(origin, key, model, folder):
    """A valid but unsatisfiable client contract must fail without delivery."""
    rows = []
    schema = {"type": "object", "properties": {"value": {"type": "integer"}},
              "required": ["value"], "not": {"required": ["value"]}}
    with httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + key}, timeout=200, trust_env=False) as client:
        for protocol in ("chat", "responses", "messages", "gemini"):
            url, body = wire(protocol, "Request the test tool once with value 7.", name="test_tool",
                             schema=schema, choice=True, stream=True)
            if protocol == "gemini":
                url = url.replace("models/auto", "models/" + model)
            else:
                body["model"] = model
            response = client.post(url, json=body)
            text = response.text
            passed = response.status_code == 200 and "error" in text and not any(
                token in text for token in ('data: [DONE]', '"finishReason":"STOP"', '"type":"message_stop"',
                                           '"type":"response.completed"', '"finish_reason":"tool_calls"'))
            rows.append({"protocol": protocol, "stream_failure": True, "passed": passed,
                         "request_id": response.headers["x-agy-request-id"]})
            (folder / f"{protocol}-failure.sse").write_text(text, encoding="utf-8")
            common.write_json(folder / "failure-results.json", rows)
            print(json.dumps(rows[-1]), flush=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", nargs="*", default=[])
    parser.add_argument("--skip-api", action="store_true")
    parser.add_argument("--failure-only", action="store_true")
    parser.add_argument("--model", default="gemini-3.8-flash-low")
    args = parser.parse_args()
    folder = ROOT / ".preview" / time.strftime("unified-cli-%Y%m%d-%H%M%S")
    folder.mkdir(parents=True)
    logging.getLogger().handlers.clear()
    logging.basicConfig(filename=folder / "proxy.log", level=logging.INFO)
    key = "local-test-" + uuid.uuid4().hex
    cfg = Config(agy_bin=shutil.which("agy") or "agy", data_dir=str(folder / "data"), api_keys=(key,),
                 password=key, turn_timeout=180, max_sessions=4, stream_heartbeat=2)
    origin, server, thread = serve(create_app(cfg))
    relay_origin, relay, relay_thread, _ = common.relay(folder, origin, key, [key])
    print("Artifacts:", folder, flush=True)
    rows = []
    try:
        if args.failure_only:
            rows.extend(failure_checks(origin, key, args.model, folder))
        elif not args.skip_api:
            rows.extend(api_checks(origin, key, args.model, folder))
        for name in args.clients:
            rows.extend(client_check(name, folder, relay_origin, key, args.model))
        common.write_json(folder / "result.json", rows)
    finally:
        relay.should_exit = True
        relay_thread.join(20)
        server.should_exit = True
        thread.join(20)
    return 0 if rows and all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
