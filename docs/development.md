# 本地开发

[返回 README](../README.md) · [配置](configuration.md) · [客户端接入](usage.md)

## 源码启动与测试

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和 [Antigravity CLI](https://antigravity.google/docs/cli/)。先运行 `agy --version` 确认安装，再运行 `agy` 完成登录；真实请求使用账号的原生凭据。网页交互登录桥需要 Linux / Docker。

```bash
uv sync
uv run main.py
```

默认监听 `127.0.0.1:8317`。配置通过进程环境传入，源码启动不会自动读取 `.env`。生产前端产物已入库，无需 Node 即可启动服务。

```bash
uv run pytest
```

协议测试使用 `tests/fake_agy.py` 等模拟器，无需真实登录。真实客户端的 MCP、图片和 Skills 验收脚本分别是 `tests/mcp_smoke.py`、`tests/vision_smoke.py`、`tests/skills_smoke.py`，需要对应客户端和账号环境；接入方式见[使用文档](usage.md)。

## 前端构建

前端使用 React 19、TypeScript、Vite、Tailwind CSS v4 和 shadcn/ui。应用入口为 `src/main.tsx`，主题位于 `src/index.css`，组件源码位于 `src/components/ui/`，shadcn 配置见 `components.json`。

```bash
npm ci
npm run build
npm run dev
```

`build` 先检查类型，再编译到 `app/static/`；`dev` 启动 Vite，将数据接口和登录 POST 请求代理到本机 8317。`npm run build:css` 是完整前端构建的兼容命令。

修改前端后重新构建，并一并提交 `app/static/index.html`、`app/static/app.css`、`app/static/assets/`。生产页面由 FastAPI 同源提供，不依赖 Node、CDN 或外部字体；Python 路由变更需要重启服务。

浏览器回归检查使用临时数据和模拟 agy：

```bash
uv run --with playwright python tests/browser_smoke.py
```

默认使用本机 Edge；其他环境可设置 `UI_BROWSER_CHANNEL=chromium`，并先安装 Playwright Chromium。

## 工作原理

```text
客户端（OpenAI / Gemini / Anthropic）
  └─ HTTP / SSE → agy-proxy（FastAPI）
                   └─ stdin / stdout NDJSON → agy（每个会话独立 PID）
```

agy 使用 `--input-format stream-json --output-format stream-json`。非流式请求在代理内汇总到本轮 `result` 后返回 JSON；流式请求发送 SSE，并在等待期间发送心跳。

- `init` 每个 CLI 会话一次，`result` 每轮一次。
- `agent_response.text_delta` 在 `ACTIVE` 和 `DONE` 中均为增量；`DONE` 仅表示当前步骤结束。
- 累计 usage 按轮差分后映射与记账，thinking 已包含在 output 中。
- 工具参数和结构化 JSON 必须等待最终结果通过完整校验后才交给客户端。
- 会话续接核对历史后发送增量；进程重建时重放完整历史，账号绑定独立持久化。

协议证据见 [Headless 分析](archive/headless-protocol-analysis-20261011.md)，会话和任务状态排查见[修正记录](archive/session-continuity-and-todos-20261011.md)。

## 延迟分析

请求日志记录分阶段耗时；流式响应末尾包含 SSE 注释 `: agy-profile ...`，非流式响应带 `X-Agy-Profile` 头。

| 阶段 | 含义 | 排查方向 |
|---|---|---|
| `models_lookup` | 模型列表查询 | 缓存命中与后台刷新 |
| `acquire` | 会话匹配或创建 | 会话关联与进程容量 |
| `agy_boot` | 新进程冷启动 | 会话复用或模型预热 |
| `think` | 首字符前的模型思考 | 模型选择、思考档位及上游负载 |
| `stream` | 内容输出 | 上游生成与连接情况 |

一次 Gemini 3.1 Pro High 测量中，冷启动约 6.3 秒、思考 28 秒、输出 7 秒；这些是单次观测值，具体耗时随模型和负载变化。

## 主要代码

| 位置 | 职责 |
|---|---|
| `app/config.py`、`app/auth.py` | 配置与鉴权 |
| `app/account_pool.py`、`app/account.py`、`app/login.py` | 多账号隔离、额度查询与登录 |
| `app/agy.py`、`app/runner.py`、`app/session_context.py` | CLI 进程、回合执行与会话关联 |
| `app/openai_api.py`、`app/responses_api.py`、`app/anthropic_api.py`、`app/gemini_api.py` | API 协议适配 |
| `app/external_tools.py`、`app/structured_output.py`、`app/tool_guard.py` | 客户端工具、结构化输出与执行限制 |
| `app/images.py`、`app/streaming.py`、`app/json_codec.py` | 图片、SSE 与 JSON 编解码 |
| `app/store.py`、`app/dashboard.py`、`app/model_policy.py` | 数据持久化、控制台与模型启停 |
| `app/update.py` | agy CLI 更新 |
| `src/`、`app/static/` | 前端源码与构建产物 |
| `tests/` | 模拟器、协议测试与客户端验收脚本 |
