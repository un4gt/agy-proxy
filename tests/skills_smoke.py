"""Exercise native client skills over the configured proxy, using uv run.

uv run python tests/skills_smoke.py --clients pi codex claude opencode
Each invocation owns its config, skill, workspace, and random reference data.
Captures and reports stay in ignored .preview; no user configuration is edited.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import mcp_smoke as common
from vision_smoke import warnings_in


SKILL_NAME = "ledger-digest"
DESCRIPTION = "Implement and validate the ledger digest workflow for this project, using the bundled contract and checker."
CHECKER = r"""import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const root=path.resolve(process.argv[2] || '.');
const contract=JSON.parse(fs.readFileSync(new URL('../references/contract.json',import.meta.url),'utf8'));
const {digest}=await import(pathToFileURL(path.join(root,'ledger.mjs')).href);
const rows=[{id:'b',cents:25},{id:'a',cents:75},{id:'z',cents:50,voided:true}];
const before=JSON.stringify(rows);
assert.deepEqual(digest(rows),{tag:contract.tag,total:100*contract.factor,ids:['a','b']});
assert.deepEqual(digest(rows,{includeVoided:true}),{tag:contract.tag,total:150*contract.factor,ids:['a','b','z']});
assert.deepEqual(digest([]),{tag:contract.tag,total:0,ids:[]});
assert.equal(JSON.stringify(rows),before);
assert.notStrictEqual(digest(rows).ids,digest(rows).ids);
fs.writeFileSync(path.join(root,'skill-receipt.json'),JSON.stringify({tag:contract.tag,checks:5}));
console.log('SKILL_CHECK_OK:'+contract.tag);
"""
INSTRUCTIONS = """---
name: ledger-digest
description: Implement and validate the ledger digest workflow for this project, using the bundled contract and checker.
---

# Ledger digest

1. Read `references/contract.json` relative to this SKILL.md, not relative to the project root.
2. Read the existing `ledger.mjs` with the client file-read tool BEFORE editing or overwriting it.
   Implement it in the current project. Export `digest(rows, {includeVoided=false}={})`.
   Return `{tag, total, ids}`. Use the exact tag and factor from the contract.
   Unless includeVoided is true, exclude rows whose voided is true.
   total is the sum of remaining cents times factor; ids are sorted ascending.
   Do not mutate the input; return a fresh ids array on every call, including empty input.
3. Run the bundled checker with the client shell tool:
   `node "<directory containing this SKILL.md>/scripts/check.mjs" "<project directory>"`.
   Resolve both placeholders to real absolute paths. Fix ledger.mjs until it passes.
4. Do not change this skill, references, or checker. The checker writes skill-receipt.json.
5. Report the checker result and the exact contract tag. Work only in this test project.

These are client-local paths and commands. No network, package installation, or other agents are needed.
"""


def fixture(work, skill):
    contract = {"tag": "账本_" + uuid.uuid4().hex[:12], "factor": 2 + uuid.uuid4().int % 17}
    (skill / "references").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "SKILL.md").write_text(INSTRUCTIONS, encoding="utf-8")
    common.write_json(skill / "references/contract.json", contract)
    (skill / "scripts/check.mjs").write_text(CHECKER, encoding="utf-8")
    (work / "ledger.mjs").write_text("export function digest() { throw new Error('Implement ledger digest'); }\n", encoding="utf-8")
    return contract, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in skill.rglob("*") if p.is_file()}


def configure(name, folder, origin, key):
    directory, work, env, command = common.setup_client(name, folder, origin, key)
    for file in ("notes.mjs", "notes.test.mjs", "requirements.md"):
        (work / file).unlink()
    cfg = directory / "config"
    if name == "pi":
        skill = cfg / "skills" / SKILL_NAME
        # Explicit resource selection excludes unrelated global skills while
        # still testing native catalog generation and automatic invocation.
        command = command[:3] + ["--provider", "agy", "--model", common.MODEL, "--thinking", "off",
            "--mode", "json", "--print", "--no-extensions", "--tools", "read,write,edit,bash",
            "--no-skills", "--skill", str(skill), "--no-context-files", "--no-prompt-templates",
            "--session", str(directory / "session.jsonl")]
        common.write_json(cfg / "mcp.json", {"mcpServers": {}})
    elif name == "codex":
        skill = work / ".agents/skills" / SKILL_NAME
        for i, value in enumerate(command):
            if value.startswith("mcp_servers="):
                command[i] = "mcp_servers={}"
        command += ["-c", "model_catalog_json=" + common.toml_value(str(common.ROOT / "examples/vision/codex-models.json")),
                    "-c", "agents.enabled=false"]
    elif name == "claude":
        skill = work / ".claude/skills" / SKILL_NAME
        for variable in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                         "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            env[variable] = common.MODEL
        common.write_json(cfg / "mcp.json", {"mcpServers": {}})
        common.write_json(cfg / "settings.json", {"disableAllHooks": True, "disableBundledSkills": True})
        command = command[:3] + ["--strict-mcp-config", "--mcp-config", str(cfg / "mcp.json"),
            "--setting-sources", "project", "--settings", str(cfg / "settings.json"),
            "--effort", "low", "--model", common.MODEL, "--print", "--verbose", "--output-format", "stream-json",
            "--permission-mode", "default", "--tools", "Skill,Read,Write,Edit,Bash",
            "--allowedTools", f"Skill({SKILL_NAME})", "Read", "Write", "Edit", "Bash(node *)"]
    else:
        skill = cfg / "skills" / SKILL_NAME
        conf = json.loads((cfg / "opencode.json").read_text(encoding="utf-8"))
        conf["mcp"] = {}
        conf["permission"] = {"*": "deny", "skill": {"*": "deny", SKILL_NAME: "allow"},
            "read": "allow", "edit": "allow", "bash": {"*": "deny", "node *": "allow"},
            "external_directory": {str(skill / "*"): "allow"}}
        common.write_json(cfg / "opencode.json", conf)
    contract, frozen = fixture(work, skill)
    return directory, work, env, command, skill, contract, frozen


def json_lines(log):
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                yield value
        except ValueError:
            continue


def execution_evidence(name, log, tag):
    """Require a real completed client command, not an assistant's claim."""
    events = list(json_lines(log))
    sentinel = "SKILL_CHECK_OK:" + tag
    if name == "pi":
        starts = {e.get("toolCallId"): e for e in events if e.get("type") == "tool_execution_start"}
        return any(e.get("type") == "tool_execution_end" and e.get("toolName") == "bash"
            and not e.get("isError") and sentinel in json.dumps(e, ensure_ascii=False)
            and "check.mjs" in json.dumps(starts.get(e.get("toolCallId"), {})) for e in events)
    if name == "codex":
        return any(e.get("type") == "item.completed" and e.get("item", {}).get("type") == "command_execution"
            and e["item"].get("exit_code") == 0 and "check.mjs" in e["item"].get("command", "")
            and sentinel in e["item"].get("aggregated_output", "") for e in events)
    if name == "claude":
        calls = {p.get("id") for e in events if e.get("type") == "assistant"
            for p in e.get("message", {}).get("content", []) if isinstance(p, dict) and p.get("type") == "tool_use"
            and p.get("name") == "Bash" and "check.mjs" in json.dumps(p.get("input", {}))}
        return any(p.get("type") == "tool_result" and p.get("tool_use_id") in calls and not p.get("is_error")
            and sentinel in json.dumps(p, ensure_ascii=False) for e in events if e.get("type") == "user"
            for p in e.get("message", {}).get("content", []) if isinstance(p, dict))
    return any(e.get("type") == "tool_use" and e.get("part", {}).get("tool") == "bash"
        and e["part"].get("state", {}).get("status") == "completed"
        and "check.mjs" in json.dumps(e["part"]["state"].get("input", {}))
        and sentinel in e["part"]["state"].get("output", "") for e in events)


def audit_inputs(directory, mode, contract):
    files = sorted((directory / "wire").glob("*-request.json"))
    inputs = [p.read_text(encoding="utf-8") for p in files]
    # OpenCode may make a separate title-generation request without tools.
    # Inspect the first actual agent request, while auditing every API response.
    agent_inputs = [json.loads(t) for t in inputs if json.loads(t).get("tools")]
    first = agent_inputs[0] if agent_inputs else {}
    # Catalog evidence must include the description and not just the prompt's
    # explicit skill name. Reference data starts only in the local skill files.
    first_text = json.dumps(first, ensure_ascii=False)
    return {"request_count": len(inputs), "agent_request_count": len(agent_inputs),
        "catalog_present": DESCRIPTION in first_text,
        "reference_absent_initially": contract["tag"] not in first_text,
        "skill_body_transmitted": any("Resolve both placeholders" in t for t in inputs),
        "reference_transmitted": any(contract["tag"] in t for t in inputs),
        "reference_tag": contract["tag"], "mode": mode}


def run_client(name, mode, folder, origin, key, secrets):
    directory, work, env, command, skill, contract, frozen = configure(name, folder, origin, key)
    prompt = "Implement the ledger digest workflow in ledger.mjs for this project. Validate it with the bundled checker and report the result."
    if mode == "explicit":
        prefix = {"pi": "/skill:", "codex": "$", "claude": "/", "opencode": "Use the skill named "}[name]
        prompt = prefix + SKILL_NAME + " " + prompt
    log = directory / "client.jsonl"
    print(f"Starting {name} skills {mode}", flush=True)
    if name in ("codex", "opencode", "claude"):
        command += ["--"]
    code = common.run_process(command + [prompt], work, env, log, secrets, timeout=900)
    unchanged = all(p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in frozen.items())
    # Run a separate verifier outside the workspace with the original reference
    # values, rather than trusting the agent's receipt or its copy of the checker.
    verifier = directory / "verify.mjs"
    original_contract = "const contract=" + json.dumps(contract, ensure_ascii=False) + ";"
    source = CHECKER.replace("const contract=JSON.parse(fs.readFileSync(new URL('../references/contract.json',import.meta.url),'utf8'));", original_contract)
    source = source.replace("fs.writeFileSync(path.join(root,'skill-receipt.json'),JSON.stringify({tag:contract.tag,checks:5}));", "")
    verifier.write_text(source, encoding="utf-8")
    verify = subprocess.run(["node", str(verifier), str(work)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    (directory / "verify.log").write_text(verify.stdout + verify.stderr, encoding="utf-8")
    audit = common.audit_log(name, log, contract["tag"], 1)
    warnings = warnings_in(log)
    result = {"client": name, "mode": mode, "exit_code": code, "fixture_unchanged": unchanged,
        "checks_pass": verify.returncode == 0, "checker_executed": execution_evidence(name, log, contract["tag"]),
        "tool_calls": audit["tool_calls"], "tool_errors": audit["tool_errors"], "warnings": warnings,
        **audit_inputs(directory, mode, contract)}
    result["passed"] = (code == 0 and unchanged and result["checks_pass"] and result["checker_executed"]
        and result["skill_body_transmitted"] and result["reference_transmitted"] and result["reference_absent_initially"]
        and (mode == "explicit" or result["catalog_present"]) and not result["tool_errors"] and not warnings)
    common.write_json(directory / "result.json", result)
    print(f"Finished {name} skills {mode}: {json.dumps(result, ensure_ascii=False)}", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", nargs="+", choices=list(common.PROTOCOLS), default=list(common.PROTOCOLS))
    parser.add_argument("--modes", nargs="+", choices=["automatic", "explicit"], default=["automatic", "explicit"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    values = {}
    for line in (common.ROOT / ".test.env").read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip("\"'")
    base, key = values["BASE_URL"].rstrip("/").removesuffix("/v1"), values["API_KEY"]
    secrets = [key, os.environ.get("EXA_API_KEY", "")]
    root = (args.output or common.ROOT / ".preview" / time.strftime("skills-%Y%m%d-%H%M%S")).resolve()
    root.mkdir(parents=True, exist_ok=False)
    results, wire = [], {}
    for mode in args.modes:
        folder = root / mode
        folder.mkdir()
        origin, server, thread, records = common.relay(folder, base, key, secrets)
        try:
            for name in args.clients:
                results.append(run_client(name, mode, folder, origin, key, secrets))
                common.write_json(root / "results.json", results)
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            common.write_json(folder / "requests.json", records)
        wire[mode] = common.audit_wire(folder, records, args.clients)
    # Every skill shares its name, but no client's transcript may contain a
    # different invocation's random contract, even when using the same API key.
    isolation = {}
    for r in results:
        directory = root / r["mode"] / r["client"]
        transcript = (directory / "client.jsonl").read_text(encoding="utf-8")
        transcript += "".join(p.read_text(encoding="utf-8") for p in (directory / "wire").glob("*"))
        other_tags = [v["reference_tag"] for v in results if v is not r]
        # Match the random ASCII suffix too, so JSON unicode escaping cannot
        # conceal another invocation's tag from this check.
        isolation[r["client"] + "/" + r["mode"]] = not any(t.split("_", 1)[-1] in transcript for t in other_tags)
    passed = all(r["passed"] for r in results) and all(isolation.values()) and all(
        v["protocol_pass"] and not v["http_errors"] and not v["stream_errors"] for group in wire.values() for v in group.values())
    common.write_json(root / "summary.json", {"passed": passed, "results": results, "wire": wire, "isolation": isolation})
    print(f"Skills summary: {root / 'summary.json'}; passed={passed}", flush=True)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
