"""Run explicitly with: uv run --with playwright python tests/browser_smoke.py.
Uses temporary storage and fake_agy, never the user's credentials or API keys.
"""
from pathlib import Path
import os
import copy
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
                browser = p.chromium.launch(channel=channel, headless=True, args=["--host-resolver-rules=MAP agy-ui.test 127.0.0.1", "--no-proxy-server"])
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.grant_permissions(["clipboard-read", "clipboard-write"])
                page = context.new_page()
                page.route("**/about", lambda r: r.fulfill(json={"service": "agy-proxy", "version": "9.8.7"}))
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(origin + "/")
                expect(page).to_have_url(origin + "/signin")
                expect(page.get_by_label("控制台密码", exact=True)).to_be_visible()
                expect(page.get_by_label("部署版本 v9.8.7").first).to_be_visible()
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
                expect(page.get_by_label("部署版本 v9.8.7")).to_be_visible()
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
                # Verify actual clipboard contents, including both fallback paths
                # while Radix keeps focus inside the secret modal.
                page.get_by_role("button", name="复制完整密钥").click()
                assert page.evaluate("navigator.clipboard.readText()") == secret
                page.evaluate("navigator.clipboard.writeText('before-unavailable')")
                page.evaluate("window.originalClipboard = navigator.clipboard; Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })")
                page.get_by_role("button", name="复制完整密钥").click()
                assert page.evaluate("window.originalClipboard.readText()") == secret
                page.evaluate("window.originalClipboard.writeText('before-denied')")
                page.evaluate("Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw new Error('Denied') } } })")
                page.get_by_role("button", name="复制完整密钥").click()
                assert page.evaluate("window.originalClipboard.readText()") == secret
                assert page.evaluate("document.querySelector('[role=dialog]').contains(document.activeElement)")
                page.evaluate("Object.defineProperty(navigator, 'clipboard', { configurable: true, value: window.originalClipboard })")
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
                account = {"id": "current", "email": "demo@example.com", "tier": "Google AI Pro", "status": "connected", "message": "已通过上游账号验证", "checked_at": time.time()}
                page.route("**/api/accounts*", lambda r: r.fulfill(json={"accounts": [account]}))
                usage = dict(account, usage_error=None, usage={"fetched_at": time.time(), "groups": [
                    {"name": "Gemini Models", "models": "Gemini Flash, Gemini Pro", "buckets": [
                        {"kind": "weekly", "label": "Weekly", "remaining_fraction": .9806, "reset_at": time.time() + 3600 * 38},
                        {"kind": "5h", "label": "Five Hour", "remaining_fraction": 1, "reset_at": None}]},
                    {"name": "Claude and GPT models", "models": "Claude Opus, Claude Sonnet, GPT-OSS", "buckets": [
                        {"kind": "weekly", "label": "Weekly", "remaining_fraction": 0, "reset_at": time.time() + 3600},
                        {"kind": "5h", "label": "Five Hour", "remaining_fraction": None, "reset_at": None}]},
                ]})
                page.route("**/api/accounts/current/usage*", lambda r: r.fulfill(json=usage))
                for route, heading in (("/login", "账号详情"), ("/update", "CLI 更新")):
                    page.goto(origin + route)
                    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
                    for width in (1440, 390):
                        page.set_viewport_size({"width": width, "height": 900})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), route
                        page.screenshot(path=str(screenshots / f"{route[1:]}-{width}.png"), full_page=True)
                page.goto(origin + "/login")
                expect(page.get_by_text("连接正常", exact=True)).to_be_visible()
                card = page.locator('[data-account-id="current"]')
                page.set_viewport_size({"width": 1440, "height": 1000})
                assert card.bounding_box()["width"] < 500, "one account must remain a compact card"
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.get_by_role("button", name="查看 Usage").click()
                    dialog = page.get_by_role("dialog")
                    expect(dialog.get_by_text("98.06%", exact=False)).to_be_visible()
                    expect(dialog.get_by_text("0.00%剩余", exact=True)).to_be_visible()
                    expect(dialog.get_by_text("未知", exact=False)).to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth"), "usage modal overflow"
                    page.screenshot(path=str(screenshots / f"usage-{width}.png"), full_page=True, animations="disabled")
                    page.get_by_role("button", name="关闭", exact=True).click()
                saved_usage = copy.deepcopy(usage)
                usage.update(status="error", message="账号查询超时，请稍后重试。", usage=None)
                page.get_by_role("button", name="查看 Usage").click()
                expect(page.get_by_text("账号查询超时，请稍后重试。").last).to_be_visible()
                expect(page.get_by_role("progressbar")).to_have_count(0)
                usage.update(saved_usage)
                page.get_by_role("button", name="刷新额度").click()
                expect(page.get_by_role("dialog").get_by_text("98.06%", exact=False)).to_be_visible()
                page.get_by_role("button", name="关闭", exact=True).click()
                # Hold a response to verify loading state and disabled refresh.
                pending_usage = []
                page.route("**/api/accounts/current/usage*", lambda r: pending_usage.append(r))
                page.get_by_role("button", name="查看 Usage").click()
                expect(page.get_by_role("status", name="正在查询额度")).to_be_visible()
                expect(page.get_by_role("button", name="刷新额度")).to_be_disabled()
                assert len(pending_usage) == 1
                pending_usage.pop().fulfill(json=usage)
                expect(page.get_by_role("dialog").get_by_text("98.06%", exact=False)).to_be_visible()
                page.get_by_role("button", name="关闭", exact=True).click()
                # Exercise the account code UI without launching an OAuth process.
                terminal = {"state": "interacting", "interactive": True, "session_id": "browser-session", "screen": [[{"text": "> Allow this connection?", "selected": True, "bold": True}]]}
                presses = []
                def handle_input(route):
                    payload = route.request.post_data_json
                    assert payload["session_id"] == "browser-session"
                    presses.append(payload["key"])
                    route.fulfill(json=terminal)
                page.route("**/login/status*", lambda r: r.fulfill(json=terminal))
                page.route("**/login/input*", handle_input)
                def handle_stop(route):
                    terminal.update(state="idle", interactive=False)
                    route.fulfill(json=terminal)
                page.route("**/login/stop*", handle_stop)
                page.goto(origin + "/login")
                page.get_by_role("button", name="登录操作").click()
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
                terminal.update(state="waiting_code", interactive=True, url="https://accounts.google.com/", tail="Waiting for code")
                def handle_code(route):
                    terminal.update(state="authenticated", message="凭据已保存，请完成其余提示")
                    route.fulfill(json=terminal)
                page.route("**/login/code*", handle_code)
                def handle_finish(route):
                    terminal.update(state="done", interactive=False)
                    route.fulfill(json=terminal)
                page.route("**/login/finish*", handle_finish)
                page.goto(origin + "/login")
                page.get_by_role("button", name="登录操作").click()
                expect(page.get_by_label("授权码", exact=True)).to_be_visible()
                page.get_by_label("授权码", exact=True).fill("fake-code")
                page.get_by_role("button", name="提交授权码").click()
                expect(page.get_by_role("button", name="完成并关闭", exact=True)).to_be_enabled()
                expect(page.get_by_role("button", name="回车确认", exact=True)).to_be_enabled()
                page.get_by_role("button", name="完成并关闭", exact=True).click()
                expect(page.get_by_role("button", name="回车确认", exact=True)).to_be_disabled()
                page.get_by_role("button", name="关闭", exact=True).click()
                # Import UI and per-account login targeting. Credentials remain
                # synthetic and the browser never sends these to upstream.
                account_list = [dict(account, id="a" * 32, name="A", isolated=True, bound_clients=1)]
                imported = []
                block_delete = [True]
                def manage_accounts(route):
                    if route.request.method == "POST":
                        payload = route.request.post_data_json
                        assert payload["name"] == "备用账号 B"
                        assert payload["credentials"] == {"token": {"access_token": "UI_FIXTURE_ONLY"}}
                        imported.append(payload)
                        added = dict(account, id="b" * 32, name=payload["name"], email=None,
                                     isolated=True, bound_clients=0, status="disconnected", message="尚未连接账号")
                        account_list.append(added)
                        route.fulfill(status=201, json={"id": added["id"]})
                    elif route.request.method == "PATCH":
                        target = next(a for a in account_list if route.request.url.endswith(a["id"]))
                        target["name"] = route.request.post_data_json["name"]
                        route.fulfill(json={"id": target["id"], "name": target["name"]})
                    elif route.request.method == "DELETE":
                        if block_delete[0]:
                            block_delete[0] = False
                            route.fulfill(status=409, json={"error": "账号正在处理请求，请等待请求结束后再删除。"})
                            return
                        target = next(a for a in account_list if route.request.url.endswith(a["id"]))
                        account_list.remove(target)
                        route.fulfill(json={"deleted": target["id"]})
                    else:
                        route.fulfill(json={"accounts": account_list})
                page.route("**/api/accounts*", manage_accounts)
                page.route("**/api/accounts/*", manage_accounts)
                page.get_by_role("button", name="添加账号", exact=True).click()
                page.get_by_label("账号名称", exact=True).fill("备用账号 B")
                page.get_by_label("凭据文件（可选）", exact=True).set_input_files({
                    "name": "account.json", "mimeType": "application/json",
                    "buffer": b'{"token":{"access_token":"UI_FIXTURE_ONLY"}}',
                })
                page.get_by_role("button", name="导入账号", exact=True).click()
                added_card = page.locator('[data-account-id="' + "b" * 32 + '"]')
                expect(added_card).to_be_visible()
                expect(added_card.get_by_text("备用账号 B", exact=True)).to_be_visible()
                assert len(imported) == 1
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 900})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(screenshots / f"multi-account-{width}.png"), full_page=True)
                with page.expect_request("**/login/status?account_id=" + "b" * 32):
                    added_card.get_by_role("button", name="连接账号", exact=True).click()
                expect(page.get_by_role("dialog").get_by_text("账号已连接", exact=True)).to_be_visible()
                page.get_by_role("button", name="关闭", exact=True).click()
                added_card.get_by_role("button", name="编辑账号 备用账号 B", exact=True).click()
                page.get_by_role("dialog").get_by_label("账号名称", exact=True).fill("   ")
                expect(page.get_by_role("button", name="保存名称", exact=True)).to_be_disabled()
                page.get_by_role("dialog").get_by_label("账号名称", exact=True).fill("备用账号已重命名")
                page.screenshot(path=str(screenshots / "edit-account-mobile.png"), full_page=True)
                page.get_by_role("button", name="保存名称", exact=True).click()
                expect(added_card.get_by_text("备用账号已重命名", exact=True)).to_be_visible()
                page.reload()
                added_card.get_by_role("button", name="删除账号 备用账号已重命名", exact=True).click()
                dialog = page.get_by_role("alertdialog")
                expect(dialog.get_by_text("后续请求会重新选择可用账号", exact=False)).to_be_visible()
                page.get_by_role("button", name="取消", exact=True).click()
                expect(added_card).to_be_visible()
                added_card.get_by_role("button", name="删除账号 备用账号已重命名", exact=True).click()
                dialog.get_by_role("button", name="确认删除", exact=True).click()
                expect(dialog.get_by_text("账号正在处理请求，请等待请求结束后再删除。", exact=True)).to_be_visible()
                expect(added_card).to_be_visible()
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 900})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(screenshots / f"delete-account-{width}.png"), full_page=True)
                dialog.get_by_role("button", name="确认删除", exact=True).click()
                expect(added_card).to_have_count(0)
                page.get_by_role("button", name="删除账号 A", exact=True).click()
                page.get_by_role("alertdialog").get_by_role("button", name="确认删除", exact=True).click()
                expect(page.get_by_text("还没有账号", exact=True)).to_be_visible()
                page.screenshot(path=str(screenshots / "accounts-empty-mobile.png"), full_page=True)
                # Real HTTP insecure context: the browser does not expose the
                # Clipboard API. Copy must still work from the generated-key modal.
                insecure_context = browser.new_context()
                insecure = insecure_context.new_page()
                insecure_origin = origin.replace("127.0.0.1", "agy-ui.test")
                insecure.goto(insecure_origin + "/signin")
                insecure.get_by_label("控制台密码", exact=True).fill("ui-test-only")
                insecure.get_by_role("button", name="登录控制台", exact=True).click()
                insecure.get_by_role("tab", name="API 密钥", exact=True).click()
                insecure.get_by_role("button", name="创建密钥", exact=True).click()
                insecure.get_by_label("备注名称").fill("HTTP copy")
                insecure.get_by_role("dialog").get_by_role("button", name="创建密钥", exact=True).click()
                expect(insecure.get_by_text("密钥已就绪")).to_be_visible()
                http_secret = insecure.get_by_role("dialog").locator("code").first.inner_text()
                assert insecure.evaluate("!isSecureContext && !navigator.clipboard")
                insecure.get_by_role("button", name="复制完整密钥").click()
                expect(insecure.get_by_text("已复制", exact=True)).to_be_visible()
                page.bring_to_front()
                assert page.evaluate("navigator.clipboard.readText()") == http_secret
                page.evaluate("navigator.clipboard.writeText('')")
                insecure_context.close()
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
                print("PASS: auth, deployment version, models, key CRUD and clipboard/fallback, account CRUD/confirmation/errors, usage, OAuth, responsive layout, no JS errors")
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


if __name__ == "__main__":
    main()
