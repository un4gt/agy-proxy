"""Persistent state for the dashboard: upstream API keys and agy usage log.

Everything lives in one JSON file (`$AGY_PROXY_DATA_DIR/data.json`, default
`~/.agy-proxy/data.json`) so a container only needs a single volume. Writes are
atomic (tmp + replace) and debounced: usage arrives on every turn, but the file
is rewritten at most once per `_SAVE_DEBOUNCE` seconds.
"""

from __future__ import annotations

import asyncio
from . import json_codec as json
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .reasoning import model_family

log = logging.getLogger("agy_proxy.store")

KEY_PREFIX = "agy-"
_SAVE_DEBOUNCE = 3.0
_REQUEST_LOG_LIMIT = 500
_MODEL_STAT_LIMIT = 40

# agy's own usage envelope field -> our normalized name. The proxy reports
# exactly what agy counts; nothing here is estimated.
_USAGE_FIELDS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("thinking_tokens", "thinking_tokens"),
    ("cache_read_tokens", "cache_read_tokens"),
    ("total_tokens", "total_tokens"),
)


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


@dataclass
class ApiKeyRecord:
    id: str
    name: str
    key: str
    created_at: float
    last_used_at: float | None = None
    requests: int = 0
    usage: dict[str, int] = field(default_factory=dict)

    def masked(self) -> str:
        return f"{self.key[:8]}…{self.key[-4:]}" if len(self.key) > 16 else self.key

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "masked": self.masked(),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "requests": self.requests,
            "usage": self.usage,
        }


@dataclass
class UsageRecord:
    ts: float
    model: str
    key_id: str | None
    key_name: str
    status: str
    duration_ms: float
    usage: dict[str, int]

    def public(self) -> dict:
        return {
            "ts": self.ts,
            "model": self.model,
            "key_name": self.key_name,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "usage": self.usage,
        }


def _empty_usage() -> dict[str, int]:
    return {name: 0 for _, name in _USAGE_FIELDS}


def normalize_usage(raw: dict | None) -> dict[str, int]:
    """Take agy's usage envelope verbatim, filling absent counters with 0."""
    out = _empty_usage()
    if not isinstance(raw, dict):
        return out
    for source, name in _USAGE_FIELDS:
        value = raw.get(source)
        if isinstance(value, (int, float)):
            out[name] = int(value)
    if not out["total_tokens"]:
        out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    return out


def add_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    merged = _empty_usage()
    for name in merged:
        merged[name] = int(left.get(name, 0)) + int(right.get(name, 0))
    return merged


class Store:
    """API keys + usage history, persisted to one JSON file."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        data_dir = Path(cfg.data_dir).expanduser() if cfg.data_dir else Path.home() / ".agy-proxy"
        self.path = data_dir / "data.json"
        self.api_keys: list[ApiKeyRecord] = []
        self.requests: deque[UsageRecord] = deque(maxlen=_REQUEST_LOG_LIMIT)
        self.models: dict[str, dict[str, Any]] = {}
        self.disabled_models: set[str] = set()
        self._lock = asyncio.Lock()
        self._dirty = False
        self._last_save = 0.0

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read %s (%s); starting with empty state", self.path, exc)
            return
        self.disabled_models = {
            model_family(model) for model in raw.get("disabled_models", [])
            if isinstance(model, str) and model
        }
        for item in raw.get("api_keys") or []:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            self.api_keys.append(
                ApiKeyRecord(
                    id=str(item.get("id") or secrets.token_hex(6)),
                    name=str(item.get("name") or "default"),
                    key=str(item["key"]),
                    created_at=float(item.get("created_at") or time.time()),
                    last_used_at=item.get("last_used_at"),
                    requests=int(item.get("requests") or 0),
                    usage=normalize_usage(item.get("usage")),
                )
            )
        for item in raw.get("requests") or []:
            try:
                self.requests.append(
                    UsageRecord(
                        ts=float(item["ts"]),
                        model=str(item.get("model") or ""),
                        key_id=item.get("key_id"),
                        key_name=str(item.get("key_name") or "—"),
                        status=str(item.get("status") or "SUCCESS"),
                        duration_ms=float(item.get("duration_ms") or 0),
                        usage=normalize_usage(item.get("usage")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        for name, stats in (raw.get("models") or {}).items():
            if isinstance(stats, dict):
                self.models[str(name)] = {
                    "requests": int(stats.get("requests") or 0),
                    "usage": normalize_usage(stats.get("usage")),
                }
        log.info("loaded %d api key(s) and %d usage record(s)", len(self.api_keys), len(self.requests))

    def _snapshot(self) -> dict:
        return {
            "version": 1,
            "api_keys": [
                {
                    "id": r.id,
                    "name": r.name,
                    "key": r.key,
                    "created_at": r.created_at,
                    "last_used_at": r.last_used_at,
                    "requests": r.requests,
                    "usage": r.usage,
                }
                for r in self.api_keys
            ],
            "requests": [r.public() | {"key_id": r.key_id} for r in self.requests],
            "models": self.models,
            "disabled_models": sorted(self.disabled_models),
        }

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_bytes(json.dumps(self._snapshot()))
            os.replace(tmp, self.path)
            self._dirty = False
            self._last_save = time.monotonic()
        except OSError as exc:
            log.warning("could not persist %s: %s", self.path, exc)

    async def save(self, *, force: bool = False) -> None:
        """Write when dirty, at most once per debounce window (unless forced)."""
        async with self._lock:
            if not self._dirty:
                return
            if not force and time.monotonic() - self._last_save < _SAVE_DEBOUNCE:
                return
            self._write()

    async def flush(self) -> None:
        await self.save(force=True)

    def model_enabled(self, model: str | None) -> bool:
        return model is None or model_family(model) not in self.disabled_models

    async def set_model_enabled(self, model: str, enabled: bool) -> None:
        async with self._lock:
            previous = self.disabled_models.copy()
            family = model_family(model)
            if enabled:
                self.disabled_models.discard(family)
            else:
                self.disabled_models.add(family)
            self._dirty = True
            self._write()
            if self._dirty:
                self.disabled_models = previous
                raise OSError("无法保存模型设置，请检查数据目录的写入权限")

    # -- api keys -----------------------------------------------------------

    def find_key(self, key: str | None) -> ApiKeyRecord | None:
        if not key:
            return None
        for record in self.api_keys:
            if secrets.compare_digest(record.key, key):
                return record
        return None

    async def create_key(self, name: str) -> ApiKeyRecord:
        record = ApiKeyRecord(
            id=secrets.token_hex(6),
            name=(name or "").strip() or "未命名",
            key=new_key(),
            created_at=time.time(),
            usage=_empty_usage(),
        )
        async with self._lock:
            self.api_keys.append(record)
            self._dirty = True
            self._write()
        return record

    async def delete_key(self, key_id: str) -> bool:
        async with self._lock:
            for index, record in enumerate(self.api_keys):
                if record.id == key_id:
                    del self.api_keys[index]
                    self._dirty = True
                    self._write()
                    return True
        return False

    async def rename_key(self, key_id: str, name: str) -> ApiKeyRecord | None:
        async with self._lock:
            for record in self.api_keys:
                if record.id == key_id:
                    record.name = (name or "").strip() or record.name
                    self._dirty = True
                    self._write()
                    return record
        return None

    # -- usage --------------------------------------------------------------

    async def record_usage(
        self,
        *,
        model: str,
        key: ApiKeyRecord | None,
        usage: dict | None,
        duration_ms: float = 0.0,
        status: str = "SUCCESS",
    ) -> None:
        normalized = normalize_usage(usage)
        record = UsageRecord(
            ts=time.time(),
            model=model or "agy-default",
            key_id=key.id if key else None,
            key_name=key.name if key else "—",
            status=status,
            duration_ms=round(float(duration_ms or 0.0), 1),
            usage=normalized,
        )
        async with self._lock:
            self.requests.append(record)
            stats = self.models.setdefault(record.model, {"requests": 0, "usage": _empty_usage()})
            stats["requests"] += 1
            stats["usage"] = add_usage(stats["usage"], normalized)
            if key is not None:
                key.requests += 1
                key.last_used_at = record.ts
                key.usage = add_usage(key.usage, normalized)
            self._dirty = True
            if time.monotonic() - self._last_save >= _SAVE_DEBOUNCE:
                self._write()

    # -- dashboard ----------------------------------------------------------

    def totals(self) -> dict:
        total = {"requests": len(self.requests), "usage": _empty_usage()}
        for record in self.requests:
            total["usage"] = add_usage(total["usage"], record.usage)
        return total

    def snapshot(self) -> dict:
        """Everything the dashboard needs in one payload."""
        total = self.totals()
        model_rows = [
            {"model": name, "requests": stats["requests"], "usage": normalize_usage(stats["usage"])}
            for name, stats in self.models.items()
        ]
        model_rows.sort(key=lambda row: (row["usage"]["total_tokens"], row["requests"]), reverse=True)
        recent = list(self.requests)[-30:][::-1]
        return {
            "totals": total,
            "models": model_rows[:_MODEL_STAT_LIMIT],
            "model_count": len(model_rows),
            "recent": [r.public() for r in recent],
            "recent_total": len(self.requests),
            "keys": [r.public() for r in self.api_keys],
            "data_path": str(self.path),
        }
