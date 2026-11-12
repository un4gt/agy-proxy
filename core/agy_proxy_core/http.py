"""Shared HTTP body reading for every protocol entry point."""
from __future__ import annotations

from fastapi import Request

from . import json_codec as json
from .agy import AgyError
from .images import MAX_REQUEST_BYTES


async def read_json(request: Request) -> dict | None:
    chunks, size = [], 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_REQUEST_BYTES:
                raise AgyError("Request body exceeds the 32 MiB limit", status_code=413)
            chunks.append(chunk)
        body = json.loads(b"".join(chunks))
    except AgyError:
        raise
    except (ValueError, UnicodeError):
        return None
    return body if isinstance(body, dict) else None
