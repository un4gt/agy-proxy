"""Wire-format helpers owned by the adapters package."""

from __future__ import annotations

from agy_proxy_core import json_codec as json
from agy_proxy_core.protocol_common import array, invalid, obj, string


def text_content(value, *, types=("text", "input_text", "output_text")) -> str:
    if isinstance(value, str):
        return value
    parts = []
    for part in array(value, "content"):
        part = obj(part, "content block")
        if part.get("type") not in types:
            raise invalid(f"Unsupported content type: {part.get('type')}; this proxy accepts text only")
        parts.append(string(part.get("text"), "content.text"))
    return "".join(parts)


def encode(event: dict) -> bytes:
    return b"event: " + event["type"].encode() + b"\ndata: " + json.dumps(event) + b"\n\n"
