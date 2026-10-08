"""External-tool protocol fixture: records conversation state, never runs tools."""

import argparse
import json
import os
import sys
from pathlib import Path

from fake_agy import emit, main as plain_main, record_invocation, usage_of


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
    assert "tools: [finish]" in Path(".agents/agents/proxy-external.md").read_text()
    schema = json.loads(Path(args.json_schema).read_text())
    conv = f"fake-tools-{os.getpid()}"
    emit({"event": "init", "conversation_id": conv,
          "init": {"agent": args.agent, "tools": ["view_file", "finish"]}})
    history = []
    for turn, line in enumerate(sys.stdin, 1):
        prompt = json.loads(line)["message"]["content"]
        if "[Client tool configuration]" not in prompt:
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
            emit({"event": "result", "result": envelope})
            continue
        config = json.loads(prompt.split("[Client tool configuration]\n")[1].split("\n\n")[0])
        fresh = "[Complete client conversation]" in prompt
        incoming = json.loads(prompt.split("conversation]\n" if fresh else "[New client messages]\n")[1]
                              .split("\n\n[Bridge output contract]\n")[0])
        history = incoming if fresh else history + incoming
        last = incoming[-1]
        text = last["content"]
        choice = config["tool_choice"]
        tools = config["tools"]
        if choice == "none" or not tools or last["role"] == "tool":
            content = f"turn={turn}; " + "; ".join(m["content"] for m in incoming)
            if text.startswith("INSPECT_CONTEXT"):
                content = json.dumps({"turn": turn, "pid": os.getpid(), "cwd": os.getcwd(),
                                      "history": history})
            output = {"kind": "message", "content": content, "tool_calls": []}
        else:
            name = choice["function"]["name"] if isinstance(choice, dict) else tools[0]["name"]
            calls = [{"name": name, "arguments": {"path": "client.txt"}}]
            if text.startswith("ECHO_ARGUMENTS\n"):
                calls[0]["arguments"] = json.loads(text.split("\n", 1)[1])
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
        emit({"event": "result", "result": envelope})


if __name__ == "__main__":
    main()
