"""Minimal HTTP client for the patched CLI's /_bridge NDJSON endpoint."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import httpx


class BridgeHTTPError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"bridge HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


class BridgeFailure(Exception):
    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _failure_status(reason: str) -> int:
    return {
        "invalid_request": 400,
        "request_too_large": 413,
        "unavailable": 503,
        "get_chat_message": 502,
        "stream": 502,
        "marshal": 502,
        "bootstrap": 503,
    }.get(reason, 502)


class BridgeClient:
    def __init__(self, url: str):
        self.url = url
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=None),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )

    async def stream(self, payload: dict) -> AsyncIterator[dict]:
        try:
            async with self._http.stream("POST", self.url, json=payload) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise BridgeHTTPError(response.status_code, body)
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        raise BridgeFailure("stream", "invalid NDJSON from bridge") from None
                    if not isinstance(event, dict):
                        raise BridgeFailure("stream", "bridge event is not an object")
                    kind = event.get("event")
                    if kind == "accepted":
                        continue
                    if kind == "response":
                        value = event.get("json")
                        if not isinstance(value, dict):
                            raise BridgeFailure("marshal", "bridge response is not JSON")
                        yield value
                    elif kind == "completed":
                        return
                    elif kind == "failed":
                        reason = event.get("reason") or "stream"
                        detail = event.get("detail")
                        raise BridgeFailure(str(reason), str(detail) if detail else None)
                    # Unknown event names are ignored for forward compatibility.
        except (httpx.HTTPError, OSError) as exc:
            raise BridgeFailure("stream", f"{type(exc).__name__}: {exc}") from exc

    async def aclose(self) -> None:
        await self._http.aclose()


async def probe(url: str) -> bool:
    """Return True when the endpoint is the bridge handler (400 + NDJSON)."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(url, content=b"{bad")
    except (httpx.HTTPError, OSError):
        return False
    content_type = response.headers.get("content-type", "")
    return response.status_code == 400 and "application/x-ndjson" in content_type
