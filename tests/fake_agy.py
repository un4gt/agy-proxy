#!/usr/bin/env python3
"""Fake `agy` CLI implementing the duplex stream-json protocol, for tests.

Supported behaviours (selected via --model):
- "error-model":  every turn ends with a result status=ERROR
- "slow-model":   sleeps 1.6s before emitting anything (idle-timeout tests)
- "crash-model":  exits with code 3 after the init event, no result
- anything else:  streams a deterministic response
A prompt of exactly "PING" answers "pong".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

MODELS = [
    "gemini-3-pro-high",
    "gemini-3-pro-low",
    "gemini-3-flash-medium",
    "gemini-3-flash-low",
    "auto",
]


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def record_invocation() -> None:
    """Optional child-process argv trace for parameter forwarding regressions."""
    path = os.environ.get("FAKE_AGY_ARGV_FILE")
    if path:
        with open(path, "a", encoding="utf-8") as output:
            output.write(json.dumps({"argv": sys.argv[1:], "pid": os.getpid()}) + "\n")


# Simulated update state, so tests can exercise the real check→download→install
# flow across processes: 9.9.9 by default, 9.9.10 after a successful "update".
def version_file() -> str:
    return os.path.join(tempfile.gettempdir(), "fake-agy-version.txt")


def fake_version() -> str:
    try:
        with open(version_file(), encoding="utf-8") as fh:
            v = fh.read().strip()
            if v:
                return v
    except OSError:
        pass
    return "9.9.9"


def usage_of(prompt: str, response: str) -> dict:
    inp = len(prompt) // 4 + 1
    out = len(response) // 4 + 1
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "thinking_tokens": 3,
        "cache_read_tokens": 0,
        "total_tokens": inp + out + 3,
    }


def respond(prompt: str, turn: int) -> str:
    if prompt.strip() == "PING":
        return "pong"
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    marker = f" [proxy {proxy}]" if proxy else ""
    snippet = prompt[:150] + " ... " + prompt[-100:] if len(prompt) > 280 else prompt
    return f"[turn {turn}] you said: {snippet}{marker}"


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "models":
        if os.environ.get("FAKE_AGY_MODELS_FAIL"):
            sys.stdout.write("\r⠋ Fetching available models...\r")
            sys.stdout.flush()
            sys.stderr.write("\x1b[KError: Please sign in to view available models.\r\n")
            sys.stderr.flush()
            raise SystemExit(1)
        print("Available models:")
        for slug in MODELS:
            print(f"  {slug}    Gemini {slug.split('-')[1] if '-' in slug else ''}")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(f"fake-agy version {fake_version()} (built for tests)")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "update":
        current = fake_version()
        print(f"⟳ Checking for updates... (current version {current})")
        if os.environ.get("FAKE_AGY_UPDATE_FAIL"):
            sys.stderr.write("Error: network unreachable\n")
            raise SystemExit(1)
        if current == "9.9.10" or os.environ.get("FAKE_AGY_UPDATE_LATEST"):
            print("✓ You are already on the latest version.")
        else:
            print("✓ Found new version 9.9.10.")
            print("⟳ Downloading update...")
            print("✓ Verification successful.")
            print("⟳ Installing update...")
            print("✓ Update successful! Please restart agy.")
            with open(version_file(), "w", encoding="utf-8") as fh:
                fh.write("9.9.10")
        return

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input-format")
    parser.add_argument("--output-format")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--agent")
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("--print-timeout")
    args, _unknown = parser.parse_known_args()
    record_invocation()

    conv = f"fake-{os.getpid()}"
    emit(
        {
            "event": "init",
            "conversation_id": conv,
            "init": {"cwd": os.getcwd(), "tools": ["run_command"], "permission_mode": "default"},
        }
    )

    if args.model == "crash-model":
        sys.stderr.write("crash-model: boom\n")
        sys.stderr.flush()
        raise SystemExit(3)

    turn = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "user":
            continue

        turn += 1
        prompt = (event.get("message") or {}).get("content") or ""

        if args.model == "error-model":
            emit(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": conv,
                        "status": "ERROR",
                        "error": "model error-model exploded",
                        "num_turns": turn,
                    },
                }
            )
            continue

        if args.model == "slow-model":
            time.sleep(1.6)

        response = respond(prompt, turn)

        # A completed tool step; its text must NOT leak into the final answer.
        emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv,
                    "step_index": 0,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "text_delta": "ls -la",
                },
            }
        )

        step_index = 1
        words = response.split(" ")
        for i, word in enumerate(words):
            fragment = word + (" " if i < len(words) - 1 else "")
            emit(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": conv,
                        "step_index": step_index,
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": fragment,
                    },
                }
            )
            time.sleep(0.005)

        # DONE carries the full cumulative step text, per the real protocol.
        emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv,
                    "step_index": step_index,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": response,
                },
            }
        )
        emit(
            {
                "event": "result",
                "result": {
                    "conversation_id": conv,
                    "status": "SUCCESS",
                    "response": response,
                    "num_turns": turn,
                    "usage": usage_of(prompt, response),
                },
            }
        )


if __name__ == "__main__":
    main()
