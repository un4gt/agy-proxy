"""Standalone agy PreToolUse hook for the external-tool bridge.

Invoked by the same Python interpreter as the proxy, without importing the app.
finish submits structured output. With a request-private manifest, view_file
or read_file may inspect only that request's validated image attachments.
"""

import orjson
import hashlib
import sys
from pathlib import Path


_IMAGE_ARG = {"view_file": "AbsolutePath", "read_file": "Path"}


def image_call_path(tool):
    """Return the image path for a supported native read, or None."""
    key = _IMAGE_ARG.get(tool.get("name"))
    args = tool.get("args")
    if key is None or not isinstance(args, dict):
        return None
    if set(args) - {key, "toolAction", "toolSummary"}:
        return None
    value = args.get(key)
    return value if isinstance(value, str) else None


def allowed_image_call(tool, paths):
    value = image_call_path(tool)
    if value is None:
        return False
    path = Path(value)
    try:
        return (value in paths and not path.is_symlink()
                and path.is_file() and str(path.resolve(strict=True)) == value)
    except OSError:
        return False


def allowed_image(tool, manifest):
    paths = orjson.loads(Path(manifest).read_text(encoding="utf-8"))
    return isinstance(paths, list) and allowed_image_call(tool, paths)


def read_marker(manifest, path):
    # Separate markers avoid lost updates when native reads run concurrently.
    return Path(manifest).parent / ("image-read-" + hashlib.sha256(path.encode()).hexdigest() + ".attempted")


def main() -> None:
    reason = ("Only finish and explicitly allowlisted image attachment reads are available here. "
              "Return external tool requests through finish; the API client executes them.")
    try:
        payload = orjson.loads(sys.stdin.buffer.read())
        tool = payload.get("toolCall", {})
        allowed = tool.get("name") == "finish"
        if len(sys.argv) == 2:
            manifest = sys.argv[1]
            if allowed:
                paths = orjson.loads(Path(manifest).read_text(encoding="utf-8"))
                allowed = isinstance(paths, list) and all(read_marker(manifest, p).is_file() for p in paths)
                if not allowed:
                    reason = ("Before finish, use view_file (or read_file) to read EVERY attachment listed in "
                              "[Image attachments], even if earlier conversation text already describes it. "
                              "Then submit finish again.")
            else:
                allowed = allowed_image(tool, manifest)
                if allowed:
                    # This records permission to attempt a read. The parent
                    # separately requires successful DONE events for every
                    # image; failed reads can never become a successful API result.
                    read_marker(manifest, image_call_path(tool)).touch(mode=0o600, exist_ok=True)
    except (ValueError, AttributeError, OSError, TypeError):
        allowed = False
    sys.stdout.buffer.write(orjson.dumps({
        "decision": "allow" if allowed else "deny",
        "reason": reason,
    }) + b"\n")


if __name__ == "__main__":
    main()
