# 本地开发

[返回 README](../README.md) · [配置](configuration.md) · [客户端接入](usage.md)

## 包结构

- `core/agy_proxy_core/`：账号、路由、隔离、store、控制台、bridge 和 Gemini 协议服务；对应发行版 `agy-proxy-core`。
- `adapters/agy_proxy_adapters/`：OpenAI Chat Completions、Responses、Anthropic Messages 适配；只依赖 core，对应发行版 `agy-proxy-adapters`。
- `main.py` 与 `python -m agy_proxy_adapters` 启动 full（core + adapters）；`python -m agy_proxy_core` 启动 core-only（只有 Gemini 协议）。
- Docker 标签 `core-vX.Y.Z` / `full-vX.Y.Z`；`latest` 指向 full。

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

协议测试使用 `FakeSession`、临时目录和模拟凭据，不需要真实登录。MCP 与 Skills 冒烟脚本分别是 `tests/mcp_smoke.py`、`tests/skills_smoke.py`，需要对应客户端和账号环境；接入方式见[使用文档](usage.md)。浏览器回归目前没有随仓库保留的 smoke 脚本，界面变更请按 README 的桌面/移动检查清单人工验收。

## 前端构建

前端使用 React 19、TypeScript、Vite、Tailwind CSS v4 和 shadcn/ui。应用入口为 `src/main.tsx`，主题位于 `src/index.css`，组件源码位于 `src/components/ui/`，shadcn 配置见 `components.json`。

```bash
npm ci
npm run build
npm run dev
```

`build` 先检查类型，再编译到 `core/agy_proxy_core/static/`；`dev` 启动 Vite，将数据接口和登录 POST 请求代理到本机 8317。`npm run build:css` 是完整前端构建的兼容命令。

修改前端后重新构建，并一并提交 `core/agy_proxy_core/static/index.html`、`core/agy_proxy_core/static/app.css`、`core/agy_proxy_core/static/assets/`。生产页面由 FastAPI 同源提供，不依赖 Node、CDN 或外部字体；Python 路由变更需要重启服务。

## 工作原理

```text
客户端（OpenAI / Gemini / Anthropic）
  └─ HTTP / SSE → agy-proxy（FastAPI）
                   └─ HTTP NDJSON /_bridge → patched agy 1.4.2（每账号一个常驻 host）
                                                └─ GetChatMessageRequest → 上游
```

镜像在构建时用 [core/agy_proxy_core/bridge/patcher.py](../core/agy_proxy_core/bridge/patcher.py) 给固定 SHA 的 1.4.2 CLI 增加 `/_bridge` HTTP 入口。代理不解析 CLI 的 stream-json 输出；每轮把完整消息历史、工具定义和配置组装成 `GetChatMessageRequest`，再把上游的 `deltaText`、`deltaToolCalls` 和 usage 转成客户端协议。流式请求发送 SSE，并在等待期间发送心跳。

- 每账号一个常驻 host，请求之间不保存对话状态；会话隔离由代理按客户端会话标识维护。
- 文本增量实时转发；工具调用等待完整参数后返回，工具由客户端执行。
- Gemini 的 `thinkingSignature` 按 tool call ID 持久化到数据目录，工具续轮时回填。
- 上游取消通过 `(*http.Request).Context()` 传递；客户端断开后停止上游请求。
- 模型目录来自 `agy models` 与固定 1.4.2 的内部枚举映射；升级 CLI 必须重新提取。

旧 stream-json/GDB 实现保存在本地分支 `legay_stream_json_version`，不参与 main 构建。

## 延迟分析

请求完成时输出一条脱敏的 `request_latency` JSON 日志，包含 `request_id`、协议、模型、账号、总耗时、attempts、marks/spans 和 usage。用响应头 `X-Agy-Request-Id` 关联客户端请求；日志不记录提示词、工具参数或模型正文。OpenAI Chat 流式响应末尾仍包含 `: agy-profile ...` 注释；Gemini SSE 不再发送注释行；非流式响应带 `X-Agy-Profile` 头。

常见 span/mark：

| 名称 | 含义 |
|---|---|
| `models_lookup` | 模型列表查询或缓存读取 |
| `session_acquire` | 选择账号并获取或启动 bridge host |
| `first_delta` | 收到首个上游文本/工具增量 |
| `session_ready` | bridge host 已可接受本轮请求 |

完整字段和导出方式以当前代码及 `request_latency` 日志为准。2026-10-14 的旧版详细评测基于已移除的 agent/stream-json 后端，归档在[链路耗时历史记录](archive/latency-observability-20261014.md)，不能直接用于当前 bridge 性能对比。

## 主要代码

| 位置 | 职责 |
|---|---|
| `core/agy_proxy_core/config.py`、`auth.py` | 配置与鉴权 |
| `core/agy_proxy_core/account_pool.py`、`account.py`、`login.py` | 多账号隔离、额度查询与登录 |
| `core/agy_proxy_core/agy.py`、`runner.py`、`session_context.py` | CLI 进程、回合执行与会话关联 |
| `core/agy_proxy_core/gemini_api.py`、`gemini_adapter.py` | Gemini 协议服务 |
| `adapters/agy_proxy_adapters/openai_api.py`、`responses_api.py`、`anthropic_api.py` | Chat Completions / Responses / Messages 适配 |
| `core/agy_proxy_core/external_tools.py`、`structured_output.py` | 客户端工具、结构化输出与执行限制 |
| `core/agy_proxy_core/images.py`、`streaming.py`、`json_codec.py` | 图片、SSE 与 JSON 编解码 |
| `core/agy_proxy_core/store.py`、`dashboard.py`、`model_policy.py` | 数据持久化、控制台与模型启停 |
| `core/agy_proxy_core/update.py` | CLI 版本状态与固定 409 的更新接口 |
| `src/`、`core/agy_proxy_core/static/` | 前端源码与构建产物 |
| `tests/`、`core/tests/`、`adapters/tests/` | 模拟器、协议测试、core/full 工厂与边界检查 |
