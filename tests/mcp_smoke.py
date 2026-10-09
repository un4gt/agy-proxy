"""Live Windows client/MCP check (uses the configured API and Exa accounts).

uv run --with 'mcp<2' python tests/mcp_smoke.py --clients pi codex claude opencode
Artifacts and credentials-free generated configs go under .preview/mcp-<timestamp>.
MCP executes in each client, never in the proxy. Nothing changes user-wide config.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
NPX = Path(r"C:\Users\MT308\AppData\Roaming\fnm\aliases\default\npx.cmd")
MODEL = "gemini-3.8-flash-low"
EXA = "https://mcp.exa.ai/mcp"
PROTOCOLS = {"pi": "/v1/chat/completions", "codex": "/v1/responses",
             "claude": "/v1/messages", "opencode": "/v1/chat/completions"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def credentials():
    values = {}
    for line in (ROOT / ".test.env").read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    if not os.environ.get("EXA_API_KEY"):
        raise SystemExit("EXA_API_KEY must be set in the current process environment")
    return values["BASE_URL"].rstrip("/").removesuffix("/v1"), values["API_KEY"]


def redact(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def fixture(work):
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", str(work)], check=True)
    (work / "notes.mjs").write_text("export function searchNotes(notes, query, options = {}) { return []; }\n", encoding="utf-8")
    (work / "requirements.md").write_text(
        "Implement searchNotes(notes, query, {limit=20}={}) in notes.mjs. "
        "Each note has id,title,body,updatedAt and optional archived. "
        "Match ALL whitespace-separated terms, case-insensitively across title and body. "
        "Exclude archived. Empty query matches all unarchived notes. Support Unicode. "
        "Sort updatedAt descending, then id ascending. Limit must be a nonnegative integer "
        "or throw RangeError; zero returns []. Return original objects without mutating input. "
        "Use no external dependencies. Run node --test.\n", encoding="utf-8")
    (work / "notes.test.mjs").write_text(TESTS, encoding="utf-8")


TESTS = """import test from 'node:test';
import assert from 'node:assert/strict';
import {searchNotes} from './notes.mjs';
const notes=[{id:'b',title:'HELLO',body:'世界 proxy',updatedAt:'2026-01-01T00:00:00Z'},
{id:'a',title:'hello 世界',body:'proxy',updatedAt:'2026-01-01T00:00:00Z'},
{id:'z',title:'hello',body:'proxy',updatedAt:'2026-02-01T00:00:00Z',archived:true}];
test('AND unicode and tie sorting',()=>assert.deepEqual(searchNotes(notes,' HELLO 世界 ').map(n=>n.id),['a','b']));
test('blank query excludes archived',()=>assert.deepEqual(searchNotes(notes,'  ').map(n=>n.id),['a','b']));
test('limits',()=>{assert.deepEqual(searchNotes(notes,'',{limit:1}).map(n=>n.id),['a']);assert.deepEqual(searchNotes(notes,'',{limit:0}),[])});
test('invalid limits',()=>{for(const limit of [-1,1.2,'2',NaN])assert.throws(()=>searchNotes(notes,'',{limit}),RangeError)});
test('no mutation original objects',()=>{const copy=JSON.stringify(notes);const out=searchNotes(notes,'hello');assert.equal(out[0],notes[1]);assert.equal(JSON.stringify(notes),copy)});
test('empty and no match',()=>{assert.deepEqual(searchNotes([],''),[]);assert.deepEqual(searchNotes(notes,'unmatched'),[])});
"""
MORE_TESTS = """
test('include archived opt-in',()=>assert.deepEqual(searchNotes(notes,'hello',{includeArchived:true}).map(n=>n.id),['z','a','b']));
test('archived limit after sorting',()=>assert.deepEqual(searchNotes(notes,'',{includeArchived:true,limit:1}).map(n=>n.id),['z']));
test('default still excludes archived',()=>assert.deepEqual(searchNotes(notes,'',{includeArchived:false}).map(n=>n.id),['a','b']));
"""


async def baseline(folder, secrets):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    work = folder / "baseline"
    work.mkdir(parents=True, exist_ok=True)
    (work / "probe.txt").write_text("MCP_STDIO_PROBE_OK", encoding="utf-8")
    result = {}
    with (folder / "baseline-stderr.log").open("w", encoding="utf-8") as err:
        async with stdio_client(StdioServerParameters(command=str(NPX), args=[
            "-y", "@modelcontextprotocol/server-filesystem", str(work)]), errlog=err) as (read, send):
            async with ClientSession(read, send) as session:
                await session.initialize()
                names = [t.name for t in (await session.list_tools()).tools]
                readback = await session.call_tool("read_text_file", {"path": str(work / "probe.txt")})
                assert not readback.isError and "MCP_STDIO_PROBE_OK" in readback.model_dump_json()
                denied = await session.call_tool("read_text_file", {"path": str(folder / "outside.txt")})
                assert denied.isError
                result["stdio"] = {"tools": names, "read": True, "outside_root_denied": True,
                                   "command": str(NPX)}
    async with streamablehttp_client(EXA, headers={"Authorization": "Bearer " + secrets[1]}) as (read, send, _):
        async with ClientSession(read, send) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            search = await session.call_tool("web_search_exa", {"query": "Node.js official test runner node --test documentation", "numResults": 2})
            assert not search.isError and "nodejs.org" in search.model_dump_json()
            result["streamable_http"] = {"url": EXA, "auth": "Authorization: Bearer ${EXA_API_KEY}",
                                         "tools": names, "search": True}
    write_json(folder / "baseline.json", result)
    print("MCP baseline: stdio read + access boundary and Exa search passed", flush=True)


def relay(folder, base, api_key, secrets):
    # Each client uses its own URL prefix so captures prove the actual wire protocol.
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=30), trust_env=False) as client:
            app.state.upstream = client
            yield

    app = FastAPI(lifespan=lifespan)
    records = []

    @app.api_route("/{client}/{path:path}", methods=["GET", "POST", "DELETE"])
    async def forward(client: str, path: str, request: Request):
        if client not in PROTOCOLS:
            return Response(status_code=404)
        raw = await request.body()
        body = json.loads(raw) if raw else {}
        index = len(records) + 1
        record = {"index": index, "client": client, "path": "/" + path, "model": body.get("model"),
                  "started": time.time(), "stream": body.get("stream"), "status": None}
        records.append(record)
        capture = folder / client / "wire"
        capture.mkdir(parents=True, exist_ok=True)
        (capture / f"{index:03}-request.json").write_text(redact(json.dumps(body, ensure_ascii=False, indent=2), secrets), encoding="utf-8")
        headers = {k: v for k, v in request.headers.items() if k.lower() in (
            "content-type", "accept", "anthropic-version", "anthropic-beta")}
        headers["authorization"] = "Bearer " + api_key
        headers["accept-encoding"] = "identity"
        url = base + "/" + path
        if request.url.query:
            url += "?" + request.url.query
        try:
            response = await app.state.upstream.send(app.state.upstream.build_request(request.method, url, headers=headers, content=raw), stream=True)
        except httpx.HTTPError as exc:
            record.update(status=502, error=type(exc).__name__)
            return Response("upstream transport error", status_code=502)
        record["status"] = response.status_code
        print(f"wire {client} /{path} -> {response.status_code}", flush=True)

        async def stream():
            captured = bytearray()
            try:
                async for chunk in response.aiter_bytes():
                    captured.extend(chunk)
                    yield chunk
            finally:
                await response.aclose()
                record["seconds"] = round(time.time() - record["started"], 2)
                (capture / f"{index:03}-response.txt").write_text(redact(captured.decode("utf-8", errors="replace"), secrets), encoding="utf-8")
                write_json(folder / "requests.json", records)

        return StreamingResponse(stream(), status_code=response.status_code,
                                 headers={"content-type": response.headers.get("content-type", "application/json")})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            return origin, server, thread, records
        time.sleep(.05)
    raise RuntimeError("relay startup timed out")


def toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(k) + " = " + toml_value(v) for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    return json.dumps(value)


def setup_client(name, folder, origin, api_key):
    directory = folder / name
    work = directory / "work"
    fixture(work)
    cfg = directory / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        env.pop(key, None)
    env.update(OPENAI_API_KEY=api_key, OPENAI_BASE_URL=f"{origin}/{name}/v1", PYTHONUTF8="1",
               PI_OFFLINE="1", PI_TELEMETRY="0")
    fs = {"command": str(NPX), "args": ["-y", "@modelcontextprotocol/server-filesystem", str(work)]}
    remote = {"url": EXA, "headers": {"Authorization": "Bearer ${EXA_API_KEY}"}}
    if name == "pi":
        env["PI_CODING_AGENT_DIR"] = str(cfg)
        write_json(cfg / "models.json", {"providers": {"agy": {
            "baseUrl": env["OPENAI_BASE_URL"], "apiKey": "OPENAI_API_KEY", "api": "openai-completions",
            "authHeader": True, "models": [{"id": MODEL, "name": "AGY Gemini Flash Low", "reasoning": False,
                                             "input": ["text"], "contextWindow": 200000, "maxTokens": 8192}]}}})
        write_json(cfg / "mcp.json", {"mcpServers": {
            "filesystem": dict(fs, exposure="direct", timeout=30), "exa": dict(remote, exposure="direct", timeout=30)}})
        write_json(cfg / "settings.json", {"quietStartup": True})
        # Do not use `npx --package ... pi`: its inherited npm_config_package
        # makes the nested filesystem npx interpret the package name as a command.
        command = [str(NPX), "--yes", "@earendil-works/pi-coding-agent@1.1.0",
                   "--provider", "agy", "--model", MODEL, "--thinking", "off", "--mode", "json", "--print",
                   "--no-extensions", "--extension", "builtin:mcp", "--tools", "bash,mcp__filesystem__*,mcp__exa__*",
                   "--no-skills", "--no-context-files", "--no-prompt-templates",
                   "--session", str(directory / "session.jsonl")]
    elif name == "codex":
        config = {"model_provider": "agy", "model": "gemini-3.8-flash", "model_reasoning_effort": "low",
                  "model_context_window": 200000, "web_search": "disabled", "approval_policy": "never",
                  "sandbox_mode": "workspace-write", "windows": {"sandbox": "unelevated"},
                  "allow_login_shell": False,
                  "model_providers": {"agy": {"name": "AGY", "base_url": env["OPENAI_BASE_URL"],
                     "env_key": "OPENAI_API_KEY", "wire_api": "responses", "supports_websockets": False}},
                  "mcp_servers": {"filesystem": dict(fs, startup_timeout_sec=30,
                      tools={tool: {"approval_mode": "approve"} for tool in ("read_text_file", "read_multiple_files", "write_file", "edit_file")}),
                     "exa": {"url": EXA, "bearer_token_env_var": "EXA_API_KEY", "startup_timeout_sec": 30}}}
        (cfg / "config.toml").write_text("\n".join(k + " = " + toml_value(v) for k, v in config.items()) + "\n", encoding="utf-8")
        command = [shutil.which("codex.cmd"), "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
                   "--json"]
        for key, value in config.items():
            command += ["-c", key + "=" + toml_value(value)]
    elif name == "claude":
        env.update(CLAUDE_CONFIG_DIR=str(cfg), ANTHROPIC_API_KEY=api_key, ANTHROPIC_BASE_URL=f"{origin}/{name}",
                   ANTHROPIC_MODEL="claude-sonnet-5-5", ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-sonnet-5-5",
                   ANTHROPIC_DEFAULT_SONNET_MODEL="claude-sonnet-5-5", ANTHROPIC_DEFAULT_OPUS_MODEL="claude-sonnet-5-5",
                   CLAUDE_CODE_SUBAGENT_MODEL="claude-sonnet-5-5", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   ENABLE_TOOL_SEARCH="false", MCP_TIMEOUT="30000", MAX_THINKING_TOKENS="0")
        write_json(cfg / "mcp.json", {"mcpServers": {"filesystem": dict(fs, type="stdio"), "exa": dict(remote, type="http")}})
        command = [str(NPX), "--yes", "@anthropic-ai/claude-code@2.1.295",
                   "--bare", "--strict-mcp-config", "--mcp-config", str(cfg / "mcp.json"),
                   "--effort", "low", "--model", "claude-sonnet-5-5", "--print", "--verbose", "--output-format", "stream-json",
                   "--allowedTools", "mcp__filesystem__*", "mcp__exa__*", "Bash(node --test*)", "--disable-slash-commands"]
    else:
        env.update(XDG_CONFIG_HOME=str(cfg), XDG_DATA_HOME=str(directory / "data"),
                   XDG_STATE_HOME=str(directory / "state"), XDG_CACHE_HOME=str(directory / "cache"),
                   OPENCODE_CONFIG_DIR=str(cfg), OPENCODE_CONFIG=str(cfg / "opencode.json"),
                   OPENCODE_DISABLE_PROJECT_CONFIG="true", OPENCODE_DISABLE_CLAUDE_CODE="true",
                   OPENCODE_DISABLE_AUTOUPDATE="true")
        write_json(cfg / "opencode.json", {"$schema": "https://opencode.ai/config.json", "autoupdate": False,
            "share": "disabled", "snapshot": False, "enabled_providers": ["agy"],
            "provider": {"agy": {"npm": "@ai-sdk/openai-compatible", "options": {
                "apiKey": "{env:OPENAI_API_KEY}", "baseURL": "{env:OPENAI_BASE_URL}"},
                "models": {MODEL: {"name": "AGY Gemini Flash Low", "limit": {"context": 200000, "output": 8192}}}}},
            "permission": {"*": "deny", "filesystem_*": "allow", "exa_*": "allow", "bash": {"*": "deny", "node --test*": "allow"}},
            "mcp": {"filesystem": {"type": "local", "command": [fs["command"], *fs["args"]], "enabled": True, "timeout": 30000},
                    "exa": {"type": "remote", "url": EXA, "oauth": False,
                            "headers": {"Authorization": "Bearer {env:EXA_API_KEY}"}, "enabled": True, "timeout": 30000}}})
        command = [shutil.which("opencode.cmd"), "run", "--pure", "--format", "json", "--model", "agy/" + MODEL,
                   "--dir", str(work)]
    return directory, work, env, command


def run_process(command, work, env, log, secrets, timeout=900):
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(command, cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
        def drain():
            for line in process.stdout:
                output.write(redact(line, secrets))
                output.flush()
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Only terminate the process tree that this test started.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            process.wait()
            code = 124
        reader.join(timeout=10)
    return code


def session_id(name, log):
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if name == "codex" and item.get("type") == "thread.started":
            return item["thread_id"]
        if name == "pi" and item.get("type") == "session":
            return item["id"]
        if name == "claude" and item.get("session_id"):
            return item["session_id"]
        if name == "opencode" and item.get("sessionID"):
            return item["sessionID"]
    return None


def audit_log(name, log, marker, phase):
    """Count completed tool calls, not model claims or echoed tool definitions."""
    calls, errors, assistant_text = [], [], []
    assertion_failures, shell_exit_codes = [], []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            if " ERROR " in line:
                errors.append(line[:300])
            continue
        kind = item.get("type")
        if name == "pi":
            if kind == "tool_execution_end":
                calls.append(item["toolName"])
                if item.get("isError"):
                    errors.append(item["toolName"])
            if kind == "message_end" and item.get("message", {}).get("role") == "assistant":
                assistant_text += [p.get("text", "") for p in item["message"].get("content", []) if p.get("type") == "text"]
        elif name == "codex" and kind == "item.completed":
            part = item["item"]
            if part.get("type") == "mcp_tool_call":
                calls.append(part.get("server", "") + "/" + part.get("tool", ""))
                if part.get("error") or (part.get("result") or {}).get("isError"):
                    errors.append(calls[-1])
            elif part.get("type") == "agent_message":
                assistant_text.append(part.get("text", ""))
            elif part.get("type") == "command_execution":
                calls.append("shell")
                shell_exit_codes.append(part.get("exit_code"))
                if part.get("exit_code") not in (None, 0):
                    if "ERR_ASSERTION" in part.get("aggregated_output", ""):
                        # A coding agent may run the new failing tests before
                        # implementing the feature. Retain this evidence; it is
                        # not an MCP/API failure. The final test run must pass.
                        assertion_failures.append({"command": part.get("command"), "exit_code": part["exit_code"]})
                    else:
                        errors.append("shell")
        elif name == "claude":
            if kind == "assistant":
                for part in item.get("message", {}).get("content", []):
                    if part.get("type") == "tool_use":
                        calls.append(part["name"])
                    elif part.get("type") == "text":
                        assistant_text.append(part.get("text", ""))
            elif kind == "user":
                for part in item.get("message", {}).get("content", []):
                    if isinstance(part, dict) and part.get("type") == "tool_result" and part.get("is_error"):
                        errors.append(part.get("tool_use_id"))
            elif kind == "result" and item.get("is_error"):
                errors.append(item.get("subtype"))
        elif name == "opencode":
            part = item.get("part", {})
            if kind == "tool_use":
                calls.append(part.get("tool", ""))
                if part.get("state", {}).get("status") == "error":
                    errors.append(calls[-1])
            elif kind == "text":
                assistant_text.append(part.get("text", ""))
        if kind in ("error", "turn.failed"):
            errors.append(kind)
    fs_read = any("filesystem" in tool and "read" in tool for tool in calls)
    fs_write = any("filesystem" in tool and ("write_file" in tool or "edit_file" in tool) for tool in calls)
    exa = any("exa" in tool and "web_search_exa" in tool for tool in calls)
    return {"tool_calls": calls, "tool_errors": errors, "filesystem_read": fs_read, "filesystem_write": fs_write,
            "exa_search": exa, "mcp_pass": fs_write and (phase == 2 or (fs_read and exa)),
            "intermediate_assertion_failures": assertion_failures,
            "last_shell_pass": not shell_exit_codes or shell_exit_codes[-1] == 0,
            "client_ran_tests": any(tool in ("bash", "Bash", "shell") for tool in calls),
            "session_marker_present": any(marker in text for text in assistant_text)}


def audit_wire(folder, records, clients):
    result = {}
    for name in clients:
        own = [r for r in records if r["client"] == name]
        errors = []
        for record in own:
            path = folder / name / "wire" / f"{record['index']:03}-response.txt"
            if not path.exists():
                errors.append({"request": record["index"], "error": "incomplete response"})
                continue
            response = path.read_text(encoding="utf-8")
            # Streaming APIs can report an error after HTTP 200 was sent.
            payloads = [line[6:] for line in response.splitlines() if line.startswith("data: ")]
            terminated = "data: [DONE]" in response.splitlines()
            if not payloads:
                payloads = [response]
            for payload in payloads:
                try:
                    value = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(value, dict) and (value.get("error") or value.get("type") in ("error", "response.failed")):
                    errors.append({"request": record["index"], "error": value})
                if isinstance(value, dict) and value.get("type") in ("message_stop", "response.completed"):
                    terminated = True
            if record.get("stream") and not terminated:
                errors.append({"request": record["index"], "error": "missing stream completion event"})
        paths = sorted({r["path"] for r in own})
        result[name] = {"requests": len(own), "paths": paths,
                        "http_errors": sum(r["status"] != 200 for r in own), "stream_errors": errors,
                        "protocol_pass": bool(own) and all(p == PROTOCOLS[name] for p in paths)}
    write_json(folder / "wire-summary.json", result)
    return result


def run_client(name, folder, origin, api_key, secrets, phases):
    directory, work, env, command = setup_client(name, folder, origin, api_key)
    version_command = command[:3] if name in ("pi", "claude") else command[:1]
    version = subprocess.run([*version_command, "--version"], cwd=work, env=env, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=90)
    if version.returncode:
        raise RuntimeError(f"{name} failed its version/startup check")
    (directory / "version.txt").write_text(redact(version.stdout.strip(), secrets) + "\n", encoding="utf-8")
    marker = name.upper() + "_" + uuid.uuid4().hex[:12]
    # Windows restricted-token sandbox cannot spawn Node's test worker. Run
    # these synchronous fixture tests in-process while retaining the sandbox.
    test_command = "node --test --test-isolation=none" if name == "codex" else "node --test"
    prompt = (f"Work only in {work}. Remember session marker {marker} for this conversation. "
              "Use the filesystem MCP tools to read requirements.md, notes.mjs, notes.test.mjs, and to write the implementation in notes.mjs. "
              "Do not use built-in file read/edit/write tools or shell for file I/O. "
              "Use the Exa MCP web_search_exa tool to find official Node.js test runner documentation (public query only, 2 results). "
              "Use filesystem MCP to write sources.md with one official nodejs.org URL returned by Exa and a brief note. "
              f"Run exactly `{test_command}` using your shell tool (this overrides the shorter command in requirements.md). "
              "Fix errors until all tests pass, without changing existing tests. "
              "Finish with a concise result and the session marker. Do the work now.")
    results = []
    first_log = directory / "phase1.jsonl"
    for phase in range(1, phases + 1):
        args = list(command)
        if phase == 2:
            (work / "notes.test.mjs").write_text(TESTS + MORE_TESTS, encoding="utf-8")
            prompt = ("Continue this same session. Add options.includeArchived=true to searchNotes so archived notes are included only when explicitly true; "
                      "keep the default exclusion and all earlier behavior. Use filesystem MCP to inspect and modify notes.mjs. "
                      f"The test file now has three extra checks; run exactly `{test_command}`. Do not modify tests. "
                      "Finish with the session marker I gave you in the previous turn, without looking for it in files.")
            sid = session_id(name, first_log)
            if name == "codex":
                args += ["resume", sid]
            elif name == "claude":
                args += ["--resume", sid]
            elif name == "opencode":
                args += ["--session", sid]
        args += [prompt]
        print(f"Starting {name} phase {phase}", flush=True)
        log = directory / f"phase{phase}.jsonl"
        code = run_process(args, work, env, log, secrets)
        # Restore the independent verifier: an agent cannot pass by weakening it.
        expected = TESTS + (MORE_TESTS if phase == 2 else "")
        tests_unchanged = (work / "notes.test.mjs").read_text(encoding="utf-8") == expected
        (work / "notes.test.mjs").write_text(expected, encoding="utf-8")
        verify = subprocess.run(["node", "--test", "notes.test.mjs"], cwd=work, capture_output=True, text=True, encoding="utf-8")
        (directory / f"phase{phase}-verify.log").write_text(verify.stdout + verify.stderr, encoding="utf-8")
        result = {"phase": phase, "exit_code": code, "tests_pass": verify.returncode == 0,
                  "tests_unchanged": tests_unchanged,
                  "sources_written": (work / "sources.md").exists(), "session_id": session_id(name, log),
                  **audit_log(name, log, marker, phase)}
        results.append(result)
        write_json(directory / "result.json", results)
        print(f"Finished {name} phase {phase}: {json.dumps(result)}", flush=True)
        if code or not result["tests_pass"] or not result["mcp_pass"]:
            break
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", nargs="*", choices=list(PROTOCOLS), default=list(PROTOCOLS))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--phases", type=int, choices=[1, 2], default=2)
    args = parser.parse_args()
    base, api_key = credentials()
    assert NPX.is_file(), "Configured npx.cmd is missing"
    folder = (args.output or ROOT / ".preview" / time.strftime("mcp-%Y%m%d-%H%M%S")).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    secrets = [api_key, os.environ["EXA_API_KEY"]]
    print(f"Artifacts: {folder}", flush=True)
    if not args.skip_baseline:
        asyncio.run(baseline(folder, secrets))
    if not args.clients:
        return
    origin, server, thread, records = relay(folder, base, api_key, secrets)
    results = {}
    try:
        for name in args.clients:
            results[name] = run_client(name, folder, origin, api_key, secrets, args.phases)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        write_json(folder / "results.json", results)
        write_json(folder / "requests.json", records)
    wire = audit_wire(folder, records, args.clients)
    wire_ok = all(r["protocol_pass"] and not r["http_errors"] and not r["stream_errors"] for r in wire.values())
    passed = wire_ok and all(len(phases) == args.phases and all(p["exit_code"] == 0 and p["tests_pass"] and p["tests_unchanged"]
                 and p["session_marker_present"] and p["sources_written"] and p["mcp_pass"] and p["client_ran_tests"]
                 and p["last_shell_pass"] and not p["tool_errors"]
                 for p in phases) for phases in results.values())
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
