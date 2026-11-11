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
                   └─ HTTP NDJSON /_bridge → patched agy 1.4.2（每账号一个常驻 host）
                                                └─ GetChatMessageRequest → 上游
```

镜像在构建时用 [app/bridge/patcher.py](../app/bridge/patcher.py) 给固定 SHA 的 1.4.2 CLI 增加 `/_bridge` HTTP 入口。代理不解析 CLI 的 stream-json 输出；每轮把完整消息历史、工具定义和配置组装成 `GetChatMessageRequest`，再把上游的 `deltaText`、`deltaToolCalls` 和 usage 转成客户端协议。流式请求发送 SSE，并在等待期间发送心跳。

- 每账号一个常驻 host，请求之间不保存对话状态；会话隔离由代理按客户端会话标识维护。
- 文本增量实时转发；工具调用等待完整参数后返回，工具由客户端执行。
- Gemini 的 `thinkingSignature` 按 tool call ID 持久化到数据目录，工具续轮时回填。
- 上游取消通过 `(*http.Request).Context()` 传递；客户端断开后停止上游请求。
- 模型目录来自 `agy models` 与固定 1.4.2 的内部枚举映射；升级 CLI 必须重新提取。

旧 stream-json/GDB 实现保存在本地分支 `legay_stream_json_version`，不参与 main 构建。

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
