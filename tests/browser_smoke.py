"""Run explicitly with: uv run --with playwright python tests/browser_smoke.py.
Uses temporary storage and fake_agy, never the user's credentials or API keys.
"""
from pathlib import Path
import os
import socket
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import uvicorn
from playwright.sync_api import sync_playwright, expect
from app import create_app
from app.config import Config


def main():
    screenshots = ROOT / ".preview" / "shadcn"
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agy-ui-") as data:
        cfg = Config(password="ui-test-only", data_dir=data, agy_bin=f'{sys.executable} {(ROOT / "tests" / "fake_agy.py").as_posix()}')
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(create_app(cfg), log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        try:
            with sync_playwright() as p:
                channel = os.environ.get("UI_BROWSER_CHANNEL", "msedge")
                browser = p.chromium.launch(channel=channel, headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(origin + "/")
                expect(page).to_have_url(origin + "/signin")
                expect(page.get_by_label("控制台密码", exact=True)).to_be_visible()
                page.screenshot(path=str(screenshots / "signin-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(screenshots / "signin-mobile.png"), full_page=True)
                page.get_by_label("控制台密码", exact=True).fill("wrong")
                page.get_by_role("button", name="登录控制台", exact=True).click()
                expect(page.get_by_text("密码不正确或连接失败，请检查后重试。")).to_be_visible()
                page.get_by_label("控制台密码", exact=True).fill("ui-test-only")
                page.get_by_role("button", name="登录控制台", exact=True).click()
                expect(page.get_by_role("heading", name="工作空间概览")).to_be_visible()
                expect(page.locator("tbody tr").first).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "dashboard overflow"
                page.screenshot(path=str(screenshots / "dashboard-mobile.png"), full_page=True)
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.screenshot(path=str(screenshots / "dashboard-desktop.png"), full_page=True)
                page.get_by_label("搜索模型").fill("no-such-model")
                expect(page.get_by_text("没有匹配的模型")).to_be_visible()
                page.get_by_label("搜索模型").fill("")
                page.get_by_role("tab", name="API 密钥", exact=True).click()
                page.get_by_role("button", name="创建密钥", exact=True).click()
                page.get_by_label("备注名称").fill("Browser smoke")
                page.get_by_role("dialog").get_by_role("button", name="创建密钥", exact=True).click()
                expect(page.get_by_text("密钥已就绪")).to_be_visible()
                secret = page.get_by_role("dialog").locator("code").first.inner_text()
                assert secret
                page.screenshot(path=str(screenshots / "key-dialog.png"), full_page=True)
                page.get_by_role("button", name="已保存", exact=True).click()
                page.get_by_role("button", name="重命名 Browser smoke", exact=True).click()
                page.get_by_label("备注名称").fill("Renamed")
                page.get_by_role("button", name="保存", exact=True).click()
                expect(page.get_by_role("button", name="删除 Renamed", exact=True)).to_be_visible()
                page.get_by_role("button", name="删除 Renamed", exact=True).click()
                page.get_by_role("button", name="取消", exact=True).click()
                expect(page.get_by_role("button", name="删除 Renamed", exact=True)).to_be_visible()
                page.get_by_role("button", name="删除 Renamed", exact=True).click()
                page.get_by_role("alertdialog").get_by_role("button", name="删除密钥", exact=True).click()
                expect(page.get_by_text("创建你的第一把密钥")).to_be_visible()
                for route, heading in (("/login", "账号连接"), ("/update", "CLI 更新")):
                    page.goto(origin + route)
                    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
                    for width in (1440, 390):
                        page.set_viewport_size({"width": width, "height": 900})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), route
                        page.screenshot(path=str(screenshots / f"{route[1:]}-{width}.png"), full_page=True)
                # Exercise the account code UI without launching an OAuth process.
                terminal = {"state": "interacting", "interactive": True, "session_id": "browser-session", "screen": [[{"text": "> Allow this connection?", "selected": True, "bold": True}]]}
                presses = []
                def handle_input(route):
                    payload = route.request.post_data_json
                    assert payload["session_id"] == "browser-session"
                    presses.append(payload["key"])
                    route.fulfill(json=terminal)
                page.route("**/login/status", lambda r: r.fulfill(json=terminal))
                page.route("**/login/input", handle_input)
                page.route("**/login/stop", lambda r: r.fulfill(json={"state": "idle", "interactive": False}))
                page.goto(origin + "/login")
                expect(page.get_by_text("CLI 交互终端", exact=True)).to_be_visible()
                page.get_by_role("button", name="↓ 下", exact=True).click()
                expect(page.get_by_role("button", name="回车确认", exact=True)).to_be_enabled()
                page.get_by_role("button", name="回车确认", exact=True).click()
                expect(page.get_by_role("button", name="↑ 上", exact=True)).to_be_enabled()
                screen = page.get_by_role("region", name="CLI 当前画面")
                screen.focus()
                screen.press("ArrowUp")
                expect(page.get_by_role("button", name="↑ 上", exact=True)).to_be_enabled()
                screen.press("Tab")
                assert presses == ["down", "enter", "up"], presses
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(screenshots / "login-interactive-mobile.png"), full_page=True)
                page.get_by_role("button", name="取消本次登录", exact=True).click()
                expect(page.get_by_role("button", name="开始连接", exact=True)).to_be_enabled()
                terminal.update(state="waiting_code", url="https://accounts.google.com/", tail="Waiting for code")
                def handle_code(route):
                    terminal.update(state="authenticated", message="凭据已保存，请完成其余提示")
                    route.fulfill(json=terminal)
                page.route("**/login/code", handle_code)
                def handle_finish(route):
                    terminal.update(state="done", interactive=False)
                    route.fulfill(json=terminal)
                page.route("**/login/finish", handle_finish)
                page.goto(origin + "/login")
                expect(page.get_by_label("授权码", exact=True)).to_be_visible()
                page.get_by_label("授权码", exact=True).fill("fake-code")
                page.get_by_role("button", name="提交授权码").click()
                expect(page.get_by_role("button", name="完成并关闭", exact=True)).to_be_enabled()
                expect(page.get_by_role("button", name="回车确认", exact=True)).to_be_enabled()
                page.get_by_role("button", name="完成并关闭", exact=True).click()
                expect(page.get_by_role("button", name="回车确认", exact=True)).to_be_disabled()
                page.get_by_role("button", name="退出登录").click()
                expect(page).to_have_url(origin + "/signin")
                page.goto(origin + "/update")
                expect(page).to_have_url(origin + "/signin")
                # No-auth branch uses the same React page.
                page.route("**/health", lambda r: r.fulfill(json={"auth_required": False}))
                page.reload()
                expect(page.get_by_text("未设置鉴权", exact=True)).to_be_visible()
                assert not errors, errors
                browser.close()
                print("PASS: login/error/logout, model search, key create/rename/delete, bridge pages, no-auth mode, responsive layout, no JS errors")
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


if __name__ == "__main__":
    main()
