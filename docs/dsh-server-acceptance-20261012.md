# 服务器更新与 DSH 编码验收（2026-10-12）

服务器已部署当前工作区的统一客户端协议实现，随后从 DSH 官方源码建立本地隔离环境，以真实 AGY 推理完成乒乓球游戏开发和同会话修复。

## 服务器更新

- 部署目录：`/home/docker_depolyments/agy-proxy`，Compose 项目名保持 `agy-proxy`。
- 当前镜像：`agy-proxy:unified-20261012`，镜像 ID 为 `sha256:fc154e3e242e5a337b1d11bba7f47a94d3fdd9b06f0fe79b91f17804376f4ad2`。
- 此为工作区构建标记；应用版本号仍为 `0.0.15`，AGY CLI 为 `1.3.3`，未推送公共镜像。
- 服务器运行文件与本地 39 个文件的 SHA-256 全部相符，容器健康、账号登录正常，恢复次数配置为 `1`。
- 保留现有 `agy-proxy_agy-gemini` 和 `agy-proxy_agy-data` 数据卷及服务器配置。
- 真实文本请求返回 `DEPLOY_OK`；Chat、Responses、Messages、Gemini 四种协议的流式工具调用、结果回传及最终回答全部通过，均有 `X-Agy-Request-Id`。

回滚 Compose 备份为 `docker-compose.yml.before-unified-20261012`，旧镜像另标记为 `agy-proxy:rollback-20261012`。需要回滚时，在部署目录恢复该 Compose 备份，并执行 `sudo docker compose up -d --no-build --pull never agy-proxy`。数据卷不应删除。

## DSH 安装与运行

环境位于仓库的 `.preview/dsh-acceptance-20261012/`，未使用 npx 安装或启动 DSH。上游不存在 `main`，实际仅有默认分支 `master`，因此安装采用：

```powershell
git clone --depth 1 --branch master https://github.com/deepseek-ai/deepseek-harness.git
cd deepseek-harness
pnpm install
pnpm run build
pnpm dsh web
```

验收固定在提交 `d743267388641bc76f17c45ce8b4c231aed1d32c`，浅克隆，源码工作区干净。Node 为 `24.19.0`，pnpm 自动使用仓库声明的 `11.28.5`；安装和完整构建均退出 `0`。已安装的 Harbor `0.24.0` 内置代理列表没有 DSH，因此本次直接使用 DSH 的 Web 和 headless profiles。

`launch.py` 从仓库 `.test.env` 读取已有代理配置，将密钥仅注入子进程环境；独立 `DSH_HOME` 为环境中的 `home/`。模型为 `gemini-3.8-flash-low`，使用 OpenAI Chat Completions 连接更新后的服务器。DSH 客户端重试设为 `0`，没有修改工具定义或增加桥接专用提示词。

Web 实际启动命令附加 `--patch apps/web/tests/pin-browse-picker.overlay.yml --port 3080 --no-open`。其认证页面 HTTP 检查通过。运行环境中的 `open-dsh.ps1` 可打开带认证的本地页面；认证链接保留在本地日志中，不纳入此报告。

## 真实编码任务

DSH 在 `pong-game/` 中从空目录生成原生 HTML/CSS/JavaScript 游戏，包含玩家键鼠控制、电脑 AI、三档难度、开始/暂停/重置、先得 5 分胜负结算、响应式 Canvas、音效和测试。

第一轮创建 8 个文件；第二轮继续同一会话，实际执行测试、修复高速球在 `dt=0.05` 时穿过挡板的问题并加入回归测试。会话 ID 为 `session-27263b9c-eb40-4adc-8341-d00e6cd520a0`。两轮合计 29 次工具调用，所有调用 ID 唯一且均有对应结果，涉及 glob、skill、todo_write、write、pwsh、read、edit。

本机首次 PowerShell 调用因测试目录缺少沙箱配置所需的 Windows 权限而失败。使用 DSH 自带诊断脚本，只为 `pong-game` 修复当前用户权限；修复已验证，恢复文件留在 `acl-recovery/`，没有改动祖先目录。随后默认沙箱可以执行命令。沙箱下 `node --test` 启动子进程被拒绝，DSH 改为 `node test/pong.test.js`，仍在相同受限模式运行真实 Node 测试；另一次未先读取文件的 edit 被拒绝后，DSH 读取并成功重试。

验收时间窗口（12:42:22–12:48:36 UTC）服务器记录 32 次成功请求，全部 attempts=1、repair_attempts=0、usage_complete=true；本轮未触发服务器参数修正。服务器记录按时间窗口统计，未假定全部外部流量均属于 DSH。

## 验证结果与复现

| 检查 | 结果 |
| --- | --- |
| DSH 内执行 Node 物理测试 | 13 通过，0 失败 |
| 独立运行 `node --test test/pong.test.js` | 13 通过，0 失败 |
| jsdom 集成检查 | 8 通过，包含开始、暂停、键鼠控制、难度、胜负、重置、缩放 |
| 游戏首页及 JS/CSS/物理模块/测试页 | HTTP 200 |
| DSH Web 认证页面 | HTTP 200 |
| 真实浏览器视觉和音效检查 | 未执行：当前浏览器连接及 Windows 自动化接口不可用 |

jsdom 中 Canvas 为替身，不能作为视觉渲染或音效通过的证据。游戏 README 经验收后修正了启动方式和测试范围说明。

当前游戏预览：`http://127.0.0.1:3081`。停止后可在 `pong-game/` 中运行 `python -m http.server 3081 --bind 127.0.0.1`。在仓库根目录可运行：

```powershell
.venv/Scripts/python.exe .preview/dsh-acceptance-20261012/launch.py web
node .preview/dsh-acceptance-20261012/ui-smoke.mjs
```

现有 Web 服务已在运行，重启前应先停止原实例。原始任务、两轮 JSON 事件、安装/构建日志、DOM 检查脚本、服务器诊断及权限恢复文件保留在测试环境中。脱敏汇总见 [结构化验收报告](reference/dsh-server-acceptance-20261012.json)。
