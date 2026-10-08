"""Interactive PTY login bridge for headless agy deployments."""

from __future__ import annotations

import codecs
import json
import logging
import os
import re
import select
import threading
import time
import uuid
from pathlib import Path

import pyte
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .agy import AgyError, _agy_argv, _child_env
from .auth import panel_authorized
from .config import Config
from .account import credentials_path

log = logging.getLogger("agy_proxy.login")
router = APIRouter()
# A terminator is required: URLs can be split across PTY reads.
_OSC8_URL_RE = re.compile(
    r"\x1b\]8;[^\x1b\x07]*;(https://accounts\.google\.com/[^\x1b\x07]*)(?:\x07|\x1b\\)"
)
_PLAIN_URL_RE = re.compile(
    r"(https://accounts\.google\.com/o/oauth2/auth\?[^\s\x1b\"']+)(?=[\s\x1b])"
)
_ACTIVE = ("starting", "interacting", "waiting_code", "exchanging", "authenticated")
_OVERALL_TTL = 900.0
_KEYS = {
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "left": b"\x1b[D",
    "right": b"\x1b[C",
    "enter": b"\r",
    "tab": b"\t",
    "shift_tab": b"\x1b[Z",
    "space": b" ",
    "escape": b"\x1b",
    "yes": b"y",
    "no": b"n",
}


def _creds_path() -> Path:
    return credentials_path()


class LoginBridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._fd: int | None = None
        self._thread: threading.Thread | None = None
        self.state = "idle"
        self.url: str | None = None
        self.message = ""
        self.session_id: str | None = None
        self._generation = 0
        self._attempt_output = ""
        self._screen = pyte.Screen(120, 40)
        self._stream = pyte.Stream(self._screen)

    def start(self) -> dict:
        with self._lock:
            if self.state in _ACTIVE:
                return self._snapshot()
            if _creds_path().exists():
                self._set("done", "已检测到登录凭据，无需重复登录。")
                return self._snapshot()
            self.state, self.url, self.message = (
                "starting",
                None,
                "正在启动，请在下方 CLI 画面中选择登录方式并确认。",
            )
            self._screen = pyte.Screen(120, 40)
            self._stream = pyte.Stream(self._screen)
            self._attempt_output = ""
            env = dict(_child_env(self.cfg) or os.environ, TERM="xterm-256color")
            try:
                argv = _agy_argv(self.cfg, [])
            except (AgyError, ValueError) as exc:
                self._set("error", f"登录命令配置无效：{exc}")
                return self._snapshot()
            try:
                import fcntl
                import pty
                import struct
                import termios
            except ImportError:
                self._set("error", "登录桥需要 Linux PTY，请在 Docker 中运行。")
                return self._snapshot()
            try:
                pid, fd = pty.fork()
            except OSError as exc:
                self._set("error", f"无法创建登录终端：{exc}")
                return self._snapshot()
            if pid == 0:
                try:
                    if self.cfg.agy_cwd:
                        os.chdir(self.cfg.agy_cwd)
                    os.execvpe(argv[0], argv, env)
                finally:
                    os._exit(127)
            self._pid, self._fd = pid, fd
            self._generation += 1
            self.session_id = uuid.uuid4().hex
            # Respond to device/cursor queries, never to permission prompts.
            self._screen.write_process_input = lambda text: self._write_unlocked(
                text.encode()
            )
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            except OSError:
                pass
            self._thread = threading.Thread(
                target=self._reader,
                args=(fd, self._generation),
                name="agy-login-pty",
                daemon=True,
            )
            self._thread.start()
            return self._snapshot()

    def send_key(self, key: str, session_id: str) -> dict:
        with self._lock:
            if key not in _KEYS:
                return self._snapshot(error="不支持的按键。")
            if not self._active_session(session_id):
                return self._snapshot(error="登录会话已结束或已更换，请刷新后重试。")
            if not self._write_unlocked(_KEYS[key]):
                return self._snapshot(error=self.message)
            return self._snapshot()

    def stop(self, session_id: str) -> dict:
        with self._lock:
            if not self._active_session(session_id):
                return self._snapshot(error="登录会话已结束或已更换。")
            self._kill_child_unlocked()
            self.url = None
            self._set("idle", "本次登录已取消，可以重新开始。")
            return self._snapshot()

    def finish(self, session_id: str) -> dict:
        with self._lock:
            if not self._active_session(session_id):
                return self._snapshot(error="登录会话已结束或已更换。")
            if not self._credentials_ready():
                return self._snapshot(error="尚未检测到完整登录凭据，请先完成授权。")
            self._kill_child_unlocked()
            self._set("done", "登录成功，凭据已保存。")
            return self._snapshot()

    def _active_session(self, session_id: str) -> bool:
        return bool(
            session_id
            and session_id == self.session_id
            and self._fd is not None
            and self.state in _ACTIVE
        )

    def submit_code(self, code: str, session_id: str | None = None) -> dict:
        with self._lock:
            if session_id is not None and not self._active_session(session_id):
                return self._snapshot(error="登录会话已结束或已更换。")
            if self.state not in ("waiting_code", "exchanging") or self._fd is None:
                return self._snapshot(error="当前没有等待授权码的登录会话。")
            code = code.strip()
            if (
                not code
                or len(code) > 8192
                or any(ord(c) < 32 or ord(c) == 127 for c in code)
            ):
                return self._snapshot(error="授权码为空、过长或包含控制字符。")
            if not self._write_unlocked((code + "\r").encode()):
                return self._snapshot(error=self.message)
            self._attempt_output = ""
            self._set("exchanging", "授权码已提交，正在交换凭据…")
            return self._snapshot()

    def status(self) -> dict:
        with self._lock:
            return self._snapshot()

    def shutdown(self) -> None:
        with self._lock:
            self._kill_child_unlocked()
            if self.state in _ACTIVE:
                self._set("idle", "登录会话已关闭。")

    def _snapshot(self, error: str | None = None) -> dict:
        screen = []
        for y in range(self._screen.lines):
            runs = []
            for x in range(self._screen.columns):
                cell = self._screen.buffer[y][x]
                # Preserve selection highlighting, even in menus without a > marker.
                selected = bool(cell.reverse or cell.bg != "default")
                if (
                    runs
                    and runs[-1]["selected"] == selected
                    and runs[-1]["bold"] == cell.bold
                ):
                    runs[-1]["text"] += cell.data
                else:
                    runs.append(
                        {"text": cell.data, "selected": selected, "bold": cell.bold}
                    )
            screen.append(runs)
        while screen and not "".join(r["text"] for r in screen[-1]).strip():
            screen.pop()
        snap = {
            "state": self.state,
            "url": self.url,
            "message": error or self.message,
            "signed_in": _creds_path().exists(),
            "session_id": self.session_id,
            "interactive": self._fd is not None and self.state in _ACTIVE,
            "screen": screen,
            "tail": "\n".join(self._screen.display).rstrip(),
        }
        if error:
            snap["error"] = error
        return snap

    def _set(self, state: str, message: str = "") -> None:
        self.state, self.message = state, message
        log.info("login bridge: %s", state)

    def _write_unlocked(self, data: bytes) -> bool:
        try:
            if self._fd is None:
                raise OSError("终端已关闭")
            while data:
                written = os.write(self._fd, data)
                if not written:
                    raise OSError("终端写入失败")
                data = data[written:]
            return True
        except OSError as exc:
            self._set("error", f"无法向登录终端发送输入：{exc}")
            self._kill_child_unlocked()
            return False

    def _reader(self, fd: int, generation: int) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buf = ""
        deadline = time.monotonic() + _OVERALL_TTL
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0.25)
            except (OSError, ValueError):
                ready = []
            with self._lock:
                if generation != self._generation:
                    return
                if time.monotonic() > deadline:
                    if self._credentials_ready():
                        self._set("done", "凭据已保存，交互终端到期关闭。")
                    else:
                        self._set("error", "登录超时（15 分钟），请重试。")
                    self._kill_child_unlocked()
                    return
                if ready:
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        if self._credentials_ready():
                            self._set("done", "登录成功，凭据已保存。")
                        else:
                            self._set(
                                "error", "agy 登录进程已退出，请查看 CLI 画面后重试。"
                            )
                        self._kill_child_unlocked()
                        return
                    text = decoder.decode(data)
                    buf = (buf + text)[-200_000:]
                    self._stream.feed(text)
                    if generation != self._generation:
                        return
                    if self.state == "starting" and any(
                        line.strip() for line in self._screen.display
                    ):
                        self._set(
                            "interacting",
                            "请查看下方 CLI 提示，用方向键选择，再按回车确认。",
                        )
                    url = self._extract_url(buf)
                    if url and url != self.url:
                        self.url = url
                        self._set(
                            "waiting_code",
                            "完成 Google 授权后，将授权码粘贴到下方提交。",
                        )
                    if self.state == "exchanging":
                        self._attempt_output = (self._attempt_output + text)[-20000:]
                        if any(
                            s in self._attempt_output.lower()
                            for s in ("token exchange failed", "invalid_grant")
                        ):
                            self._set(
                                "waiting_code",
                                "授权码无效；如 CLI 提示按键继续，请手动按回车，然后重新提交授权码。",
                            )
                if (
                    self.state in _ACTIVE
                    and self.state != "authenticated"
                    and self._credentials_ready()
                ):
                    self._set(
                        "authenticated",
                        "凭据已保存。请处理 CLI 中剩余的确认提示，完成后点击「完成并关闭」。",
                    )

    @staticmethod
    def _credentials_ready() -> bool:
        # Do not kill agy while it has opened but not finished writing the file.
        try:
            return bool(json.loads(_creds_path().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return False

    @staticmethod
    def _extract_url(text: str) -> str | None:
        matches = list(_OSC8_URL_RE.finditer(text)) or list(
            _PLAIN_URL_RE.finditer(text)
        )
        return matches[-1].group(1) if matches else None

    def _kill_child_unlocked(self) -> None:
        self._generation += 1
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
            try:
                os.waitpid(self._pid, 0)
            except ChildProcessError:
                pass
            self._pid = None


class KeyInput(BaseModel):
    key: str
    session_id: str


class LoginSession(BaseModel):
    session_id: str


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
        body = {}
        code = ""
    return JSONResponse(
        _bridge(request).submit_code(code, (body or {}).get("session_id"))
    )


@router.get("/login/status")
async def login_status(request: Request):
    denied = _auth_error(request)
    if denied is not None:
        return denied
    return JSONResponse(_bridge(request).status())


@router.post("/login/input")
async def login_input(request: Request, body: KeyInput):
    denied = _auth_error(request)
    if denied is not None:
        return denied
    result = _bridge(request).send_key(body.key, body.session_id)
    return JSONResponse(result, status_code=400 if "error" in result else 200)


@router.post("/login/stop")
async def login_stop(request: Request, body: LoginSession):
    denied = _auth_error(request)
    if denied is not None:
        return denied
    result = _bridge(request).stop(body.session_id)
    return JSONResponse(result, status_code=409 if "error" in result else 200)


@router.post("/login/finish")
async def login_finish(request: Request, body: LoginSession):
    denied = _auth_error(request)
    if denied is not None:
        return denied
    result = _bridge(request).finish(body.session_id)
    return JSONResponse(result, status_code=409 if "error" in result else 200)
