"""Standalone agy PreToolUse hook for the external-tool bridge.

Invoked by the same Python interpreter as the proxy, without importing the app.
finish submits structured output. With a request-private manifest, view_file
may inspect only that request's validated image attachments.
"""

import json
import hashlib
import sys
from pathlib import Path


def allowed_image(tool, manifest):
    args = tool.get("args")
    if tool.get("name") != "view_file" or not isinstance(args, dict):
        return False
    if set(args) - {"AbsolutePath", "toolAction", "toolSummary"}:
        return False
    value = args.get("AbsolutePath")
    if not isinstance(value, str):
        return False
    paths = json.loads(Path(manifest).read_text(encoding="utf-8"))
    path = Path(value)
    return (isinstance(paths, list) and value in paths and not path.is_symlink()
            and path.is_file() and str(path.resolve(strict=True)) == value)


def read_marker(manifest, path):
    # Separate markers avoid lost updates when native reads run concurrently.
    return Path(manifest).parent / ("image-read-" + hashlib.sha256(path.encode()).hexdigest() + ".attempted")


def main() -> None:
    reason = ("Only finish and explicitly allowlisted image attachment reads are available here. "
              "Return external tool requests through finish; the API client executes them.")
    try:
        payload = json.load(sys.stdin)
        tool = payload.get("toolCall", {})
        allowed = tool.get("name") == "finish"
        if len(sys.argv) == 2:
            manifest = sys.argv[1]
            if allowed:
                paths = json.loads(Path(manifest).read_text(encoding="utf-8"))
                allowed = isinstance(paths, list) and all(read_marker(manifest, p).is_file() for p in paths)
                if not allowed:
                    reason = ("Before finish, use view_file to read EVERY attachment listed in [Image attachments], "
                              "even if earlier conversation text already describes it. Then submit finish again.")
            else:
                allowed = allowed_image(tool, manifest)
                if allowed:
                    # This records permission to attempt a read. The parent
                    # separately requires successful DONE events for every
                    # image; failed reads can never become a successful API result.
                    read_marker(manifest, tool["args"]["AbsolutePath"]).touch(mode=0o600, exist_ok=True)
    except (ValueError, AttributeError, OSError, TypeError):
        allowed = False
    print(json.dumps({
        "decision": "allow" if allowed else "deny",
        "reason": reason,
    }))


if __name__ == "__main__":
    main()
