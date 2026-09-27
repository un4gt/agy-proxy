"""Standalone agy PreToolUse hook for the external-tool bridge.

Invoked by the same Python interpreter as the proxy, without importing the app.
Only finish is a local operation: it submits structured output to the caller.
"""

import json
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        allowed = payload.get("toolCall", {}).get("name") == "finish"
    except (ValueError, AttributeError):
        allowed = False
    print(json.dumps({
        "decision": "allow" if allowed else "deny",
        "reason": "Only finish is available here. Return external tool requests through finish; "
                  "the API client executes them and supplies the results.",
    }))


if __name__ == "__main__":
    main()
