"""In-container agy CLI self-update bridge.

Runs the official non-interactive `agy update` subcommand (check → download →
verify → install) as a background task and streams its progress to a small
page at /update, so a container deployment can upgrade agy without rebuilding
the image. Idle chat sessions are closed first (they would otherwise keep
serving the old binary until TTL expiry); busy turns are left to finish.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .agy import _agy_argv, _child_env
from .auth import panel_authorized
from .config import Config

log = logging.getLogger("agy_proxy.update")

router = APIRouter()

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# "⟳ Checking for updates... (current version 1.2.1)"
_CURRENT_VERSION_RE = re.compile(r"current version\s+v?([0-9][\w.\-]*)")
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

_UPDATE_TIMEOUT = 600.0  # whole update budget (check+download+install), seconds
_VERSION_TIMEOUT = 15.0


def _clean(raw: bytes) -> str:
    return _ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).replace("\r", "").strip()


class UpdateBridge:
    """Single-flight driver for `agy update` (check → download → install)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = "idle"  # idle|running|done|error
        self.message = ""
        self.output: list[str] = []
        self.version: str | None = None  # cached `agy --version`
        self.before_version: str | None = None
        self.after_version: str | None = None
        self.sessions_killed = 0
        self.busy_sessions = 0
        self._task: asyncio.Task | None = None
        self._version_task: asyncio.Task | None = None
        self._version_failed = False
        self._proc: asyncio.subprocess.Process | None = None

    # -- public API (called from routes) ------------------------------------

    def start(self, *, sessions_killed: int, busy_sessions: int) -> dict:
        if self.state == "running":
            return self.snapshot()
        self.state = "running"
        self.message = "正在检查更新…"
        self.output = []
        self.before_version = None
        self.after_version = None
        self.sessions_killed = sessions_killed
        self.busy_sessions = busy_sessions
        self._task = asyncio.create_task(self._run(), name="agy-update")
        return self.snapshot()

    async def status(self) -> dict:
        # One-shot `agy --version` so the page can show the current version.
        # Await it briefly so a single fetch (the chat-page badge) already
        # carries the version; retried on later polls only until it succeeds.
        if self.version is None and not self._version_failed:
            if self._version_task is None or self._version_task.done():
                self._version_task = asyncio.create_task(self._load_version(), name="agy-version")
            if not self._version_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(self._version_task), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
        return self.snapshot()

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "message": self.message,
            "version": self.version,
            "before_version": self.before_version,
            "after_version": self.after_version,
            "sessions_killed": self.sessions_killed,
            "busy_sessions": self.busy_sessions,
            "output": "\n".join(self.output)[-4000:],
        }

    async def shutdown(self) -> None:
        for task in (self._task, self._version_task):
            if task is not None:
                task.cancel()
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

    # -- internals ----------------------------------------------------------

    async def _run(self) -> None:
        argv = _agy_argv(self.cfg, ["update"])
        log.info("agy update started: %s", argv)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=_child_env(self.cfg),
                cwd=self.cfg.agy_cwd or None,
            )
        except (FileNotFoundError, PermissionError) as exc:
            self._set("error", f"无法启动 agy（{self.cfg.agy_bin!r}）: {exc}")
            return
        self._proc = proc
        rc: int | None = None
        try:
            assert proc.stdout is not None
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=_UPDATE_TIMEOUT)
                if not line:
                    break
                text = _clean(line)
                if text:
                    self.output.append(text)
                    match = _CURRENT_VERSION_RE.search(text)
                    if match:
                        self.before_version = match.group(1)
            rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            self._set("error", f"更新超时（{_UPDATE_TIMEOUT:.0f}s），已中止，请重试。")
            return
        finally:
            self._proc = None

        if rc != 0:
            tail = " | ".join(self.output[-3:])[-300:]
            self._set("error", f"agy update 失败（rc={rc}）: {tail or '无输出'}")
            return

        self.after_version = await self._read_version()
        self.version = self.after_version or self.version
        if any("already on the latest version" in line for line in self.output):
            suffix = f"v{self.after_version}" if self.after_version else "版本未知"
            self._set("done", f"agy 已是最新版本（{suffix}），无需更新。")
        else:
            old, new = self.before_version, self.after_version
            arrow = f"v{old} → v{new}" if old and new else (f"v{new}" if new else "")
            self._set(
                "done",
                f"更新成功 ✓ {arrow}，新会话自动使用新版本，无需重启容器"
                + (f"（已结束 {self.sessions_killed} 个空闲会话）" if self.sessions_killed else "")
                + (f"；{self.busy_sessions} 个进行中的对话将用完旧版后自然结束"
                   if self.busy_sessions else ""),
            )

    async def _load_version(self) -> None:
        self.version = await self._read_version()
        if self.version is None:
            self._version_failed = True

    async def _read_version(self) -> str | None:
        try:
            argv = _agy_argv(self.cfg, ["--version"])
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_child_env(self.cfg),
                cwd=self.cfg.agy_cwd or None,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_VERSION_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return None
        text = _clean(out)
        match = _VERSION_RE.search(text)
        return match.group(0) if match else (text or None)

    def _set(self, state: str, message: str) -> None:
        self.state = state
        self.message = message
        log.info("update bridge: %s %s", state, message)


# -- routes ------------------------------------------------------------------

def _bridge(request: Request) -> UpdateBridge:
    bridge = getattr(request.app.state, "update_bridge", None)
    if bridge is None:
        bridge = UpdateBridge(request.app.state.cfg)
        request.app.state.update_bridge = bridge
    return bridge


def _auth_error(request: Request) -> JSONResponse | None:
    """None if allowed; otherwise a 401 for the panel password."""
    if panel_authorized(request.app.state.cfg, request):
        return None
    return JSONResponse(
        status_code=401,
        content={"error": {"message": "面板密码无效或缺失"}},
    )


@router.get("/update", response_class=HTMLResponse)
async def update_page(request: Request):
    if not panel_authorized(request.app.state.cfg, request):
        return RedirectResponse("/signin", status_code=307)
    from .ui import ui_page
    return ui_page()


@router.post("/update/start")
async def update_start(request: Request):
    denied = _auth_error(request)
    if denied is not None:
        return denied
    bridge = _bridge(request)
    if bridge.state == "running":
        return JSONResponse(bridge.snapshot())
    # Free the old binary: idle sessions are cheap to respawn, and on Windows
    # a running process blocks its own replacement.
    sessions = request.app.state.sessions
    killed = await sessions.close_idle()
    return JSONResponse(bridge.start(sessions_killed=killed, busy_sessions=sessions.busy_count()))


@router.get("/update/status")
async def update_status(request: Request):
    denied = _auth_error(request)
    if denied is not None:
        return denied
    return JSONResponse(await _bridge(request).status())
