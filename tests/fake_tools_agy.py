"""External-tool protocol fixture: records conversation state, never runs tools."""

import argparse
import json
import hashlib
import os
import sys
import time
from pathlib import Path

from fake_agy import emit, main as plain_main, record_invocation, usage_of, account_failure


def main():
    if "--json-schema" not in sys.argv:
        return plain_main()
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-schema")
    parser.add_argument("--agent")
    args, unknown = parser.parse_known_args()
    record_invocation()
    assert "--dangerously-skip-permissions" not in unknown
    assert "--disable-slash-commands" in unknown
    assert Path(".agents/hooks.json").exists()
    image_paths = json.loads(Path("image-permissions.json").read_text()) if Path("image-permissions.json").exists() else []
    expected_tools = "tools: [finish, view_file]" if image_paths else "tools: [finish]"
    agent = Path(".agents/agents/proxy-external.md").read_text(encoding="utf-8")
    assert expected_tools in agent
    config = (json.JSONDecoder().raw_decode(agent.split("[Client tool configuration]\n", 1)[1])[0]
              if "[Client tool configuration]\n" in agent else None)
    schema = json.loads(Path(args.json_schema).read_text())
    conv = f"fake-tools-{os.getpid()}"
    emit({"event": "init", "conversation_id": conv,
          "init": {"agent": args.agent, "tools": ["view_file", "finish"]}})
    history = []
    cumulative_usage = {}
    for turn, line in enumerate(sys.stdin, 1):
        prompt = json.loads(line)["message"]["content"]
        # Model a persistent agent configuration, not declarations supplied by
        # each user turn. Regressions must fail even if a duplicate is identical.
        assert "[Client tool configuration]\n" not in prompt
        trace = os.environ.get("FAKE_AGY_TURNS_FILE")
        if trace:
            with open(trace, "a", encoding="utf-8") as output:
                output.write(json.dumps({"pid": os.getpid(), "turn": turn, "prompt": prompt,
                                         "agent": agent if turn == 1 else None}) + "\n")
        if account_failure(prompt):
            continue
        if "SLOW_OUTPUT" in prompt:
            time.sleep(0.6)
        repair = "[Validation feedback]" in prompt
        if repair and "SLOW_REPAIR" in prompt:
            time.sleep(0.6)

        def emit_result(envelope):
            for key, value in envelope.get("usage", {}).items():
                cumulative_usage[key] = cumulative_usage.get(key, 0) + value
            envelope.update(conversation_id=conv, num_turns=turn, usage=dict(cumulative_usage))
            emit({"event": "result", "result": envelope})
        image_hashes = []
        for image_path in image_paths:
            image_hashes.append(hashlib.sha256(Path(image_path).read_bytes()).hexdigest())
            if "SKIP_IMAGE_READ" not in prompt:
                emit({"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE",
                      "tool_info": {"name": "view_file", "parameters": {
                          "AbsolutePath": "/etc/passwd" if "WRONG_IMAGE_READ" in prompt else image_path}}}})
        if config is None:
            output = {"name": "Budi", "age": 20}
            if "BAD_TYPE" in prompt:
                output["age"] = "twenty"
            if "EXTRA_FIELD" in prompt:
                output["extra"] = True
            emit({"event": "step_update", "step_update": {
                "step_type": "agent_response", "state": "ACTIVE", "text_delta": "not valid JSON"}})
            if "NATIVE_TOOL" in prompt:
                emit({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "run_command"}})
            envelope = {"status": "SUCCESS", "structured_output": output,
                        "response": "not JSON", "usage": usage_of(prompt, json.dumps(output))}
            if "MALFORMED" in prompt:
                envelope.pop("structured_output")
            if "SCHEMA_ECHO" in prompt:
                envelope["structured_output"] = {"schema": schema, "prompt": prompt}
            if "json" in schema.get("properties", {}) and "structured_output" in envelope:
                envelope["structured_output"] = {"json": json.dumps(output)}
                if "JSON_ARRAY" in prompt:
                    envelope["structured_output"] = {"json": "[]"}
                if "JSON_INVALID" in prompt:
                    envelope["structured_output"] = {"json": "not JSON"}
            emit_result(envelope)
            continue
        fresh = "[Complete client conversation]" in prompt
        incoming = json.JSONDecoder().raw_decode(prompt.split(
            "[Complete client conversation]\n" if fresh else "[New client messages]\n", 1)[1])[0]
        history = incoming if fresh else history + incoming
        last = incoming[-1]
        text = last["content"]
        choice = config["tool_choice"]
        tools = config["tools"]
        if choice == "none" or not tools or last["role"] == "tool":
            content = f"turn={turn}; " + "; ".join(m["content"] for m in incoming)
            if text.startswith("INSPECT_CONTEXT"):
                content = json.dumps({"turn": turn, "pid": os.getpid(), "cwd": os.getcwd(),
                                      "history": history, "image_hashes": image_hashes, "image_paths": image_paths,
                                      "image_path_fingerprints": [hashlib.sha256(p.encode()).hexdigest() for p in image_paths]})
            output = {"kind": "message", "content": content, "tool_calls": []}
        else:
            name = choice["function"]["name"] if isinstance(choice, dict) else tools[0]["name"]
            calls = [{"name": name, "arguments": {"path": "client.txt"}}]
            if text.startswith("ECHO_ARGUMENTS\n"):
                calls[0]["arguments"] = json.loads(text.split("\n", 1)[1])
            if text.startswith("REPAIR_ARGS\n"):
                calls[0]["arguments"] = json.loads(text.split("\n", 1)[1]) if repair else {}
            if text.startswith("SLOW_REPAIR"):
                calls[0]["arguments"] = {"path": "fixed.txt"} if repair else {}
            if text == "MULTI":
                calls.append({"name": name, "arguments": {"path": "second.txt"}})
            if text == "BAD_ARGS":
                calls[0]["arguments"] = {"path": 42}
            if text == "BAD_JSON":
                calls[0]["arguments"] = '{oops'
            if text == "JSON_STRING_ARGS":
                calls[0]["arguments"] = '{"path": "client.txt"}'
            if text == "ARRAY_ARGS":
                calls[0]["arguments"] = [{"path": "client.txt"}]
            if text == "NULL_ARGS":
                calls[0]["arguments"] = None
            if text == "UNKNOWN_TOOL":
                calls[0]["name"] = "undeclared"
            output = {"kind": "tool_calls", "content": "", "tool_calls": calls}
            if text == "NO_CALL":
                output = {"kind": "message", "content": "done", "tool_calls": []}
            if text == "WRONG_KIND":
                output["kind"] = "message"
        if text == "NATIVE_TOOL":
            emit({"event": "step_update", "step_update": {"step_type": "tool",
                  "tool_name": "view_file", "state": "ACTIVE"}})
        # This internal JSON must not appear in OpenAI text deltas.
        emit({"event": "step_update", "step_update": {"step_type": "agent_response",
              "state": "DONE", "step_index": turn, "text_delta": json.dumps(output)}})
        emit({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "finish"}})
        envelope = {"status": "SUCCESS", "conversation_id": conv, "num_turns": turn,
                    "response": "this is not the structured response", "structured_output": output,
                    "usage": usage_of(prompt, json.dumps(output))}
        if text == "MALFORMED":
            envelope.pop("structured_output")
        emit_result(envelope)


if __name__ == "__main__":
    main()
