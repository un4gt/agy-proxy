"""PTY interaction tests; real terminal cases run on Linux / Docker."""

import os
import sys
import time
from pathlib import Path

import pytest

from app.config import Config
from app.login import LoginBridge, _KEYS


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    import app.login as login

    monkeypatch.setattr(login, "_creds_path", lambda: tmp_path / "oauth_creds.json")
    b = LoginBridge(Config())
    yield b
    b.shutdown()


def test_keys_are_allowlisted_and_require_live_session(bridge, monkeypatch):
    sent = []
    monkeypatch.setattr(os, "write", lambda fd, data: sent.append(data) or len(data))
    assert "error" in bridge.send_key("enter", "session")
    bridge._fd, bridge.session_id, bridge.state = 123456, "session", "interacting"
    assert "error" in bridge.send_key("down", "stale")
    assert "error" in bridge.send_key("rm -rf /", "session")
    assert not sent
    for key, data in _KEYS.items():
        assert "error" not in bridge.send_key(key, "session")
        assert sent[-1] == data
    bridge._fd = None


def test_render_current_screen_and_selection(bridge):
    bridge._stream.feed(
        "old content\x1b[2J\x1b[HChoose:\r\n\x1b[7mAllow\x1b[0m\r\nDeny"
    )
    snap = bridge.status()
    assert "old content" not in snap["tail"]
    assert "Allow" in snap["tail"]
    assert any(
        run["selected"] and "Allow" in run["text"]
        for row in snap["screen"]
        for run in row
    )


def test_terminal_queries_get_responses_without_auto_consent(bridge):
    replies = []
    bridge._screen.write_process_input = replies.append
    bridge._stream.feed("\x1b[c\x1b[6n")
    assert replies == ["\x1b[?6c", "\x1b[1;1R"]
    assert "\r" not in replies


def test_incomplete_oauth_url_is_not_exposed():
    prefix = "\x1b]8;;https://accounts.google.com/o/oauth2/auth?state=abc"
    assert LoginBridge._extract_url(prefix) is None
    assert LoginBridge._extract_url(prefix + "def\x1b\\").endswith("abcdef")


def test_code_rejects_control_sequences(bridge, monkeypatch):
    sent = []
    monkeypatch.setattr(os, "write", lambda fd, data: sent.append(data) or len(data))
    bridge._fd, bridge.session_id, bridge.state = 123456, "session", "waiting_code"
    for code in ("", "abc\x1b[A", "a\nb", "a" * 8193):
        assert "error" in bridge.submit_code(code, "session")
    assert "error" in bridge.submit_code("valid", "stale")
    assert not sent
    bridge.submit_code("valid", "session")
    assert sent == [b"valid\r"]
    bridge._fd = None


def wait_for(bridge, predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snap = bridge.status()
        if predicate(snap):
            return snap
        time.sleep(0.02)
    raise AssertionError(bridge.status())


@pytest.mark.skipif(os.name == "nt", reason="Linux PTY required")
def test_real_pty_menu_selection_retry_and_completion(bridge, tmp_path):
    script = tmp_path / "menu.py"
    script.write_text(r"""import os,sys,tty,json,time
from pathlib import Path
tty.setraw(0)
def emit(text):
 os.write(1,text.encode())
def exact(data):
 got=b""
 while len(got)<len(data):got+=os.read(0,len(data)-len(got))
 if got!=data:raise SystemExit(3)
emit("\x1b[c")
exact(b"\x1b[?6c")
emit("\x1b[2J\x1b[HMenu: Deny / Allow\r\n"+os.getcwd()+"\r\n"+os.environ.get("HTTPS_PROXY",""))
exact(b"\x1b[B")
emit("\x1b[2J\x1b[H\x1b[7mAllow selected\x1b[0m")
exact(b"\r")
emit("\x1b[2J\x1b[HPress space then Enter to agree")
exact(b" ");exact(b"\r")
url="\x1b]8;;https://accounts.google.com/o/oauth2/auth?state=abcdef\x1b\\Open\x1b]8;;\x1b\\"
for char in url:
 emit(char);time.sleep(.001)
exact(b"bad\r")
emit("\r\ninvalid_grant. Press Enter to retry")
exact(b"\r")
emit("\x1b[2J\x1b[HNew code:")
exact(b"good\r")
Path("oauth_creds.json").write_text("{")
time.sleep(.4)
Path("oauth_creds.json").write_text(json.dumps({"access_token":"test-only"}))
emit("\r\nFinal consent: use Up then Enter")
exact(b"\x1b[A");exact(b"\r")
emit("\r\nReady")
time.sleep(5)
""")
    bridge.cfg = Config(
        agy_bin=f"{sys.executable} {script}",
        agy_cwd=str(tmp_path),
        http_proxy="http://proxy.test:7890",
    )
    session = bridge.start()["session_id"]
    snap = wait_for(bridge, lambda s: "Menu:" in s["tail"])
    assert snap["interactive"] and snap["state"] == "interacting"
    assert str(tmp_path) in snap["tail"] and "http://proxy.test:7890" in snap["tail"]
    time.sleep(0.1)  # no unsolicited confirmation should have advanced the menu
    assert "Menu:" in bridge.status()["tail"]
    bridge.send_key("down", session)
    snap = wait_for(bridge, lambda s: "Allow selected" in s["tail"])
    assert any(r["selected"] for row in snap["screen"] for r in row)
    bridge.send_key("enter", session)
    wait_for(bridge, lambda s: "Press space" in s["tail"])
    bridge.send_key("space", session)
    bridge.send_key("enter", session)
    snap = wait_for(bridge, lambda s: s["state"] == "waiting_code")
    assert snap["url"].endswith("abcdef")
    bridge.submit_code("bad", session)
    wait_for(bridge, lambda s: s["state"] == "waiting_code" and "无效" in s["message"])
    bridge.send_key("enter", session)
    wait_for(bridge, lambda s: "New code:" in s["tail"])
    bridge.submit_code("good", session)
    snap = wait_for(bridge, lambda s: s["state"] == "authenticated")
    assert snap["interactive"]
    bridge.send_key("up", session)
    bridge.send_key("enter", session)
    wait_for(bridge, lambda s: "Ready" in s["tail"])
    snap = bridge.finish(session)
    assert snap["state"] == "done"
    assert not snap["interactive"] and bridge._pid is None
    assert "error" in bridge.send_key("enter", session)


@pytest.mark.skipif(os.name == "nt", reason="Linux PTY required")
def test_cancel_restart_and_timeout(bridge, tmp_path, monkeypatch):
    script = tmp_path / "wait.py"
    script.write_text("import time; print('Waiting',flush=True); time.sleep(30)")
    bridge.cfg = Config(agy_bin=f"{sys.executable} {script}")
    first = bridge.start()["session_id"]
    wait_for(bridge, lambda s: "Waiting" in s["tail"])
    assert bridge.stop(first)["state"] == "idle"
    second = bridge.start()["session_id"]
    assert first != second
    assert "error" in bridge.send_key("enter", first)
    wait_for(bridge, lambda s: "Waiting" in s["tail"])
    assert bridge.status()["interactive"]  # old reader must not kill the new one
    bridge.stop(second)
    monkeypatch.setattr("app.login._OVERALL_TTL", 0.1)
    bridge.start()
    snap = wait_for(bridge, lambda s: s["state"] == "error")
    assert "超时" in snap["message"] and not snap["interactive"]
