"""In-container agy OAuth login bridge.

Drives the `agy` TUI sign-in screen through a PTY, extracts the Google OAuth
URL, and exposes it (plus a code-paste box) at GET /login so a browser on the
user's own machine can complete authorization for a headless container.
"""

from __future__ import annotations

import logging
import os
import re
import select
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .auth import panel_authorized
from .config import Config

log = logging.getLogger("agy_proxy.login")

router = APIRouter()

# Full URL lives inside an OSC-8 hyperlink escape sequence (the visible
# text is line-wrapped/clipped, the hyperlink payload is complete).
_OSC8_URL_RE = re.compile(r"\x1b\]8;[^\x1b\x07]*;(https://accounts\.google\.com/[^\x1b\x07]*)")
_PLAIN_URL_RE = re.compile(r"(https://accounts\.google\.com/o/oauth2/auth\?[^\s\x1b\"']+)")
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[>=][0-9;?]*[A-Za-z]|\x1b[=>]"
)

_ACTIVE = ("starting", "waiting_code", "exchanging")
_OVERALL_TTL = 900.0  # whole login session budget, seconds
_ENTER_DELAY = 6.0  # press Enter on the sign-in button after this long


def _creds_path() -> Path:
    return Path.home() / ".gemini" / "oauth_creds.json"


class LoginBridge:
    """Single-flight PTY driver for the agy TUI sign-in flow."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._fd: int | None = None
        self._thread: threading.Thread | None = None
        self.state = "idle"  # idle|starting|waiting_code|exchanging|done|error
        self.url: str | None = None
        self.message = ""
        self.output_tail: deque[str] = deque(maxlen=60)
        self._deadline = 0.0

    # -- public API (called from routes) ------------------------------------

    def start(self) -> dict:
        with self._lock:
            if self.state in _ACTIVE:
                return self._snapshot()
            if _creds_path().exists():
                self.state = "done"
                self.message = "已登录过（检测到凭据文件），无需重复登录。"
                return self._snapshot()
            self.state = "starting"
            self.url = None
            self.message = ""
            self.output_tail.clear()

            env = dict(os.environ, TERM="xterm-256color")
            try:
                import fcntl
                import pty
                import struct
                import termios
            except ImportError:
                self.state = "error"
                self.message = "登录桥需要 Linux PTY，请在 Docker 中运行。"
                return self._snapshot()
            try:
                pid, fd = pty.fork()
            except OSError as exc:
                self.state = "error"
                self.message = f"failed to allocate a PTY: {exc}"
                return self._snapshot()
            if pid == 0:  # child: exec agy TUI
                try:
                    os.execvpe("agy", ["agy"], env)
                finally:
                    os._exit(127)
            self._pid, self._fd = pid, fd
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            except OSError:
                pass
            self._deadline = time.monotonic() + _OVERALL_TTL
            self._thread = threading.Thread(target=self._reader, name="agy-login-pty", daemon=True)
            self._thread.start()
            return self._snapshot()

    def submit_code(self, code: str) -> dict:
        with self._lock:
            if self.state not in ("waiting_code", "exchanging"):
                return self._snapshot(error="No login is waiting for a code.")
            code = code.strip()
            if not code:
                return self._snapshot(error="Empty code.")
            try:
                os.write(self._fd, (code + "\r").encode("utf-8"))
            except OSError as exc:
                self.state = "error"
                self.message = f"failed to write code to agy: {exc}"
                return self._snapshot()
            self.state = "exchanging"
            self.message = "授权码已提交，正在交换凭据…"
            return self._snapshot()

    def status(self) -> dict:
        with self._lock:
            return self._snapshot()

    def shutdown(self) -> None:
        with self._lock:
            self._kill_child_unlocked()

    # -- internals ----------------------------------------------------------

    def _snapshot(self, error: str | None = None) -> dict:
        snap = {
            "state": self.state,
            "url": self.url,
            "message": self.message or error or "",
            "signed_in": _creds_path().exists(),
            "tail": "".join(self.output_tail)[-2000:],
        }
        if error:
            snap["error"] = error
        return snap

    def _set(self, state: str, message: str = "") -> None:
        self.state = state
        self.message = message
        log.info("login bridge: %s %s", state, message)

    def _reader(self) -> None:
        buf = b""
        enter_sent = False
        started = time.monotonic()
        fd = self._fd
        while True:
            if time.monotonic() > self._deadline:
                with self._lock:
                    self._set("error", "登录超时（15 分钟），请重试。")
                    self._kill_child_unlocked()
                return
            try:
                ready, _, _ = select.select([fd], [], [], 1.0)
            except (OSError, ValueError):
                break
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                buf = (buf + data)[-200_000:]
                text = data.decode("utf-8", errors="replace")
                with self._lock:
                    self.output_tail.append(_ANSI_RE.sub("", text))

            with self._lock:
                if self.state == "starting":
                    if not enter_sent and time.monotonic() - started >= _ENTER_DELAY:
                        enter_sent = True
                        try:
                            os.write(fd, b"\r")  # activate "Sign in" button
                        except OSError:
                            pass
                    url = self._extract_url(buf.decode("utf-8", errors="replace"))
                    if url:
                        self.url = url
                        self._set("waiting_code", "在自己电脑上完成 Google 授权后，把授权码粘贴到下方提交。")

                if self.state == "exchanging":
                    text_all = buf.decode("utf-8", errors="replace")
                    if "token exchange failed" in text_all or "invalid_grant" in text_all:
                        # Bad code: the TUI stops at "press any key"; send one and
                        # go back to the code input so the user can retry.
                        try:
                            os.write(fd, b"\r")
                        except OSError:
                            pass
                        self._set(
                            "waiting_code",
                            "授权码无效（token exchange failed），请重新从回调页复制 code 再试。",
                        )

                if self.state in ("waiting_code", "exchanging") and _creds_path().exists():
                    self._set("done", "登录成功，凭据已保存 ✓")
                    time.sleep(1.0)  # let agy flush credentials
                    self._kill_child_unlocked()
                    return

        # EOF / exit
        with self._lock:
            if self.state != "done":
                if _creds_path().exists():
                    self._set("done", "登录成功 ✓")
                else:
                    self._set("error", "agy 进程意外退出，请展开下方输出查看原因后重试。")
            self._kill_child_unlocked()

    @staticmethod
    def _extract_url(text: str) -> str | None:
        match = _OSC8_URL_RE.search(text)
        if match:
            return match.group(1)
        match = _PLAIN_URL_RE.search(text)
        return match.group(1) if match else None

    def _kill_child_unlocked(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._pid is not None:
            try:
                os.kill(self._pid, 9)
            except ProcessLookupError:
                pass
            self._pid = None


# -- routes ------------------------------------------------------------------

def _bridge(request: Request) -> LoginBridge:
    bridge = getattr(request.app.state, "login_bridge", None)
    if bridge is None:
        bridge = LoginBridge(request.app.state.cfg)
        request.app.state.login_bridge = bridge
    return bridge


def _auth_error(request: Request) -> JSONResponse | None:
    """None if allowed; otherwise a 401 for the panel password."""
    if panel_authorized(request.app.state.cfg, request):
        return None
    return JSONResponse(
        status_code=401,
        content={"error": {"message": "面板密码无效或缺失"}},
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    from fastapi.responses import RedirectResponse

    if not panel_authorized(request.app.state.cfg, request):
        return RedirectResponse("/signin", status_code=307)
    from .ui import ui_page
    return ui_page()


@router.post("/login/start")
async def login_start(request: Request):
    denied = _auth_error(request)
    if denied is not None:
        return denied
    return JSONResponse(_bridge(request).start())


@router.post("/login/code")
async def login_code(request: Request):
    denied = _auth_error(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
        code = str((body or {}).get("code", ""))
    except Exception:
        code = ""
    return JSONResponse(_bridge(request).submit_code(code))


@router.get("/login/status")
async def login_status(request: Request):
    denied = _auth_error(request)
    if denied is not None:
        return denied
    return JSONResponse(_bridge(request).status())
