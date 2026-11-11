"""Persistent patched-CLI hosts for the native bridge.

One process is kept per account.  The process only provides authentication,
the HTTP server and the real CodeAssistClient; each request is independent and
carries the full conversation.  Process lifetime is therefore much simpler
than a duplex CLI session: no per-conversation CLI state lives in the process.
The stream-json flags only put the pinned CLI into headless mode so the
patched HTTP host starts; chat messages do not use the stream-json protocol.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import aclosing
from collections import OrderedDict, deque
from pathlib import Path
from typing import AsyncIterator

from ..account_pool import AccountPoolError
from ..agy import AgyError, _agy_argv, _child_env
from .client import BridgeClient, BridgeFailure, BridgeHTTPError, probe

log = logging.getLogger("agy_proxy.bridge.session")


def listening_ports(pid: int) -> list[int]:
    """Find LISTEN sockets owned by pid using /proc (Linux only)."""
    inodes: set[str] = set()
    fd_dir = f"/proc/{pid}/fd"
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return []
    for name in names:
        try:
            link = os.readlink(os.path.join(fd_dir, name))
        except OSError:
            continue
        if link.startswith("socket:[") and link.endswith("]"):
            inodes.add(link[8:-1])
    if not inodes:
        return []
    ports: set[int] = set()
    for table in ("tcp", "tcp6"):
        try:
            with open(f"/proc/{pid}/net/{table}", encoding="ascii") as fh:
                next(fh, None)
                for line in fh:
                    fields = line.split()
                    if len(fields) < 10 or fields[3] != "0A" or fields[9] not in inodes:
                        continue
                    try:
                        ports.add(int(fields[1].rsplit(":", 1)[1], 16))
                    except (ValueError, IndexError):
                        continue
        except OSError:
            continue
    return sorted(ports)


class BridgeSession:
    def __init__(self, cfg, *, account_id: str, account_name: str = ""):
        self.cfg = cfg
        self.account_id = account_id
        self.account_name = account_name
        self.proc: asyncio.subprocess.Process | None = None
        self.client: BridgeClient | None = None
        self.url: str | None = None
        self.inflight = 0
        self.turns = 0
        self.created_at = 0.0
        self.last_used = 0.0
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_task: asyncio.Task | None = None
        self._dead = False

    @property
    def alive(self) -> bool:
        return (not self._dead and self.proc is not None
                and self.proc.returncode is None and self.client is not None)

    @property
    def busy(self) -> bool:
        return self.inflight > 0

    async def start(self) -> None:
        if os.name == "nt":
            raise AgyError("native bridge requires Linux", status_code=503)
        argv = _agy_argv(self.cfg, ["--input-format", "stream-json",
                                    "--output-format", "stream-json"])
        cwd = self.cfg.agy_cwd or None
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(self.cfg),
                cwd=cwd,
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise AgyError(f"failed to start bridge host ({self.cfg.agy_bin!r}): {exc}",
                           status_code=503) from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="bridge-stderr")
        try:
            await self._wait_listener()
        except BaseException:
            await self.kill()
            raise
        log.info("bridge host started pid=%s account=%s url=%s", self.proc.pid,
                 self.account_name or self.account_id, self.url)

    async def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.stderr_tail.append(text)
        except asyncio.CancelledError:
            raise

    def _stderr_text(self) -> str:
        return " | ".join(list(self.stderr_tail)[-3:])

    async def _wait_listener(self) -> None:
        deadline = asyncio.get_running_loop().time() + 30.0
        while True:
            if self.proc is None or self.proc.returncode is not None:
                raise AgyError(
                    f"bridge host exited during startup (code {self.proc.returncode if self.proc else '?'}): "
                    + self._stderr_text(), status_code=503)
            ports = await asyncio.to_thread(listening_ports, self.proc.pid)
            for port in ports:
                url = f"http://127.0.0.1:{port}/_bridge"
                if await probe(url):
                    self.url = url
                    self.client = BridgeClient(url)
                    return
            if asyncio.get_running_loop().time() >= deadline:
                raise AgyError("bridge host did not expose /_bridge within 30s: "
                               + self._stderr_text(), status_code=503)
            await asyncio.sleep(0.2)

    async def stream(self, payload: dict) -> AsyncIterator[dict]:
        if not self.alive or self.client is None:
            raise AgyError("bridge host is not running", status_code=502)
        self.inflight += 1
        self.turns += 1
        self.last_used = asyncio.get_running_loop().time()
        try:
            async with aclosing(self.client.stream(payload)) as events:
                async for event in events:
                    yield event
        except BridgeHTTPError as exc:
            raise AgyError(f"bridge rejected the request (HTTP {exc.status})",
                           status_code=503 if exc.status >= 500 else 400) from exc
        except BridgeFailure as exc:
            log.warning("bridge failure reason=%s detail=%s", exc.reason, exc.detail)
            if exc.reason in ("invalid_request", "request_too_large"):
                raise AgyError(f"bridge request rejected: {exc.detail or exc.reason}",
                               status_code=400 if exc.reason == "invalid_request" else 413,
                               code=exc.reason) from exc
            if exc.reason == "get_chat_message":
                raise AgyError("CLI upstream request failed", status_code=502,
                               code="cli_upstream_error") from exc
            raise AgyError("bridge stream failed", status_code=502,
                           code="cli_transport_error", retryable=True) from exc
        finally:
            self.inflight = max(0, self.inflight - 1)
            self.last_used = asyncio.get_running_loop().time()

    async def kill(self) -> None:
        self._dead = True
        if self.client is not None:
            try:
                await self.client.aclose()
            except Exception:
                log.debug("error closing bridge client", exc_info=True)
            self.client = None
        if self.proc is not None and self.proc.returncode is None:
            if self.proc.stdin is not None:
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    self.proc.kill()
                    await self.proc.wait()
                except ProcessLookupError:
                    pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def close(self) -> None:
        await self.kill()


class BridgeSessionManager:
    """One bridge host per account, bounded by the existing session limit."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._sessions: dict[str, BridgeSession] = {}
        self._lock = asyncio.Lock()
        self._signatures: OrderedDict[str, str] = OrderedDict()
        self._signature_dirty = False
        data_dir = Path(cfg.data_dir).expanduser() if cfg.data_dir else Path.home() / ".agy-proxy"
        self._signature_path = data_dir / "bridge-signatures.json"
        self._load_signatures()

    def _load_signatures(self) -> None:
        try:
            raw = json.loads(self._signature_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return
        cutoff = time.time() - 7 * 24 * 3600
        entries = raw.get("signatures")
        if not isinstance(entries, dict):
            return
        for key, entry in entries.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            value = entry.get("value")
            ts = entry.get("ts")
            if isinstance(value, str) and isinstance(ts, (int, float)) and ts >= cutoff:
                self._signatures[key] = value
        while len(self._signatures) > 4096:
            self._signatures.popitem(last=False)

    def remember_signature(self, key: str, value: str | None) -> None:
        if not key or not value:
            return
        self._signatures[key] = value
        self._signatures.move_to_end(key)
        self._signature_dirty = True
        while len(self._signatures) > 4096:
            self._signatures.popitem(last=False)

    def signature(self, key: str) -> str | None:
        return self._signatures.get(key)

    def flush_signatures(self) -> None:
        if not self._signature_dirty:
            return
        now = time.time()
        payload = {
            "version": 1,
            "saved_at": now,
            "signatures": {key: {"value": value, "ts": now}
                           for key, value in self._signatures.items()},
        }
        tmp = self._signature_path.with_name(self._signature_path.name + ".tmp")
        try:
            self._signature_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._signature_path)
            self._signature_dirty = False
        except OSError:
            log.warning("could not persist bridge signatures", exc_info=True)
            try:
                tmp.unlink()
            except OSError:
                pass

    async def acquire(self, *, model: str | None, effort: str | None = None,
                      context=None, owner: str | None = None, **_ignored) -> BridgeSession:
        pool = self.cfg.account_pool
        if owner is None:
            owner = (context.owner or context.key) if context is not None else None
        profile = None
        if pool is not None:
            try:
                profile = pool.select(owner, model)
                if profile is not None:
                    pool.prepare(profile)
            except (AccountPoolError, OSError) as exc:
                raise AgyError(str(exc), status_code=503) from None
        account_id = profile.id if profile is not None else "current"
        account_name = profile.name if profile is not None else ""
        cfg = profile.configured(self.cfg) if profile is not None else self.cfg
        async with self._lock:
            session = self._sessions.get(account_id)
            if session is not None and session.alive:
                session.last_used = asyncio.get_running_loop().time()
                return session
            if session is not None:
                await session.close()
                self._sessions.pop(account_id, None)
            while len(self._sessions) >= max(1, self.cfg.max_sessions):
                victim = next((s for s in self._sessions.values() if not s.busy), None)
                if victim is None:
                    raise AgyError("All bridge request slots are busy; retry later", status_code=429)
                await victim.close()
                self._sessions.pop(victim.account_id, None)
            fresh = BridgeSession(cfg, account_id=account_id, account_name=account_name)
            await fresh.start()
            fresh.last_used = asyncio.get_running_loop().time()
            self._sessions[account_id] = fresh
            return fresh

    async def release(self, session: BridgeSession | None, *, reply=None) -> None:
        if session is None:
            return
        session.last_used = asyncio.get_running_loop().time()
        if not session.alive:
            async with self._lock:
                self._sessions.pop(session.account_id, None)

    async def sweep(self) -> None:
        now = asyncio.get_running_loop().time()
        async with self._lock:
            for account_id, session in list(self._sessions.items()):
                if not session.busy and (not session.alive
                                         or now - session.last_used > self.cfg.session_ttl):
                    await session.close()
                    self._sessions.pop(account_id, None)
        await asyncio.to_thread(self.flush_signatures)

    async def ensure_prewarm(self) -> None:
        return None

    async def close_all(self) -> None:
        async with self._lock:
            for session in list(self._sessions.values()):
                await session.close()
            self._sessions.clear()
        await asyncio.to_thread(self.flush_signatures)

    async def close_idle(self) -> int:
        async with self._lock:
            killed = 0
            for account_id, session in list(self._sessions.items()):
                if not session.busy:
                    await session.close()
                    self._sessions.pop(account_id, None)
                    killed += 1
            return killed

    async def remove_account(self, account_id: str, *, login_bridges=None, other_managers=()) -> dict:
        async with self._lock:
            sessions = [s for s in self._sessions.values() if s.account_id == account_id]
            if any(s.busy for s in sessions):
                raise AgyError("账号正在处理请求，请等待请求结束后再删除。", status_code=409)
            login_bridge = (login_bridges or {}).get(account_id)
            if login_bridge is not None and login_bridge.active:
                raise AgyError("账号正在登录，请先完成或取消登录后再删除。", status_code=409)
            for session in sessions:
                await session.close()
                self._sessions.pop(session.account_id, None)
            pool = self.cfg.account_pool
            if pool is None:
                raise AgyError("账号池未启用", status_code=400)
            result = pool.remove(account_id)
            if login_bridge is not None:
                login_bridge.shutdown()
            return result

    def busy_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.alive and s.busy)

    @property
    def live_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.alive)
