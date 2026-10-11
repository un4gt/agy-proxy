# agy-proxy

把 [Antigravity CLI（`agy`）](https://antigravity.google/docs/cli/headless/) 的 **stream-json 双向模式**包装成标准 API 服务的反向代理：

- **内置控制台**：打开首页即可看 agy 当前可用模型、token 用量（取自 agy 自己的 usage 计数），并**创建/吊销上游 API Key**
- **Gemini API** 兼容：`POST /v1beta/models/{model}:generateContent`、`:streamGenerateContent`（SSE）、`GET /v1beta/models`、`:countTokens`
- **OpenAI API** 兼容：`POST /v1/chat/completions`（支持流式、`stream_options.include_usage` 和客户端 `tools/tool_calls`）、`GET /v1/models`
- **图片输入**：Chat Completions、Responses、Anthropic Messages 支持用户附图、图文混排和多图
- **容器内 agy 自更新**：`/update` 页面一键升级容器内的 agy CLI，无需重建镜像

```bash
docker pull tumuer/agy-proxy:v0.0.14
```

任何支持自定义 base URL 的客户端（Cherry Studio、OpenWebUI、LobeChat、Continue、openai/gemini SDK 等）都能直接接入。

## 工作原理

```
客户端 (OpenAI / Gemini 格式)
   │  HTTP + SSE
   ▼
agy-proxy (FastAPI)
   │  stdin  ← NDJSON: {"event":"user","message":{"content":"..."}}
   │  stdout → NDJSON: init / step_update(text_delta) / result
   ▼
agy --input-format stream-json --output-format stream-json  (每个 agent 会话独立 PID)
```

- CLI 始终使用 stdin/stdout NDJSON。客户端 `stream=true` 接收 SSE，`stream=false` 在代理内汇总到本轮 `result` 后返回 JSON
- `agent_response.text_delta` 在 `ACTIVE` 和 `DONE` 中都是增量；`DONE` 只表示这一步结束，不代表整轮结束
- `init` 每个 CLI 会话一次，`result` 每轮一次。累计 usage 按轮差分后再映射和记账；thinking 已包含在 output 中
- HTTP SSE 在生成期间每 10 秒发送保活。工具参数/结构化 JSON 必须等待最终结果完整校验后才能交给客户端

### Agent 会话与进程对应

适用于任意 coding agent / agent 客户端，包括 dsh、pi、Codex、Claude Code、OpenCode。**服务端维护会话与独立 AGY PID / conversation_id 的对应关系，客户端无需新增自定义请求头。** 默认在同一 API 凭据内，按完整历史与上一轮实际回复（包括工具调用 ID）识别连续请求。

客户端若已有会话字段，优先使用它；兼容 `X-Agy-Session-Id`、`X-Session-Id`、`session-id`、`session_id`、`X-Session-Affinity`、`X-Opencode-Session`，以及 `metadata.session_id`、Codex 的 `client_metadata.session_id/thread_id`、Claude Code 的 JSON `metadata.user_id.session_id`。同时出现多个标识时必须一致；普通用户名、请求 ID、API Key 或相同首条 prompt 不会把多个会话合并。

- 客户端仍提交完整历史，代理核对已确认历史后只向同一 stdin 发送新消息。工具回传包含上一条 assistant 的调用 ID，便于关联真实工具结果
- 单个 PID 同时只执行一轮；显式同会话重叠请求或自动识别出的在途重复续接返回 409，容量全部忙时返回 429
- 没有会话字段也会保留成功进程。唯一匹配到连续历史才复用；首次请求、匹配歧义或并发分支各自建立独立进程
- 历史重试/编辑/截断、模型/effort/工具/schema/附件变化、进程失败或 TTL/LRU 回收时，使用完整历史建立新进程。失败和取消的半轮状态不会交给下一次请求
- 成功且可复用的进程保持 stdin 打开；空闲回收时先关闭 stdin，让 CLI 正常退出。外部工具仍仅在客户端执行，私有工作目录/图片路径校验持续有效

`prompt_cache_key` 本身可能跨会话共享，默认不作为身份；连续历史仍可自动复用。如果客户端原本支持明确的会话标识，可以消除内容完全一致的会话或克隆之间的歧义；无需为 dsh/pi 修改 headers 或缓存配置。协议证据与接入说明见 [Headless 分析](docs/headless-protocol-analysis-20261011.md)，自动会话关联和任务状态排查见 [本次修正记录](docs/session-continuity-and-todos-20261011.md)。

## 前置条件

1. 已安装 Antigravity CLI：`agy --version`（[安装文档](https://antigravity.google/docs/cli/)）
2. **先交互式登录一次**：终端运行 `agy`，完成 OAuth。headless 模式复用本机凭据
   （未登录时所有请求会返回 502 及 agy 的认证错误信息）

## 启动

```bash
uv sync
uv run main.py            # 等价于 uvicorn app:app --host 127.0.0.1 --port 8317
```

## 多账号隔离与固定账号路由

在「账号详情 → 添加账号」中导入 agy 原生凭据文件（`antigravity-oauth-token`），或包含 `token` 的 agy-switch 完整账号 JSON；也可以先创建空账号，再在其卡片中完成授权（交互登录桥需要 Linux / Docker）。「导入本机当前凭据」读取服务端的登录文件。首次添加新账号时，已有的文件凭据会先迁移为「原服务账号」，保留原 CLI 安装标识；原文件不改动。凭据导入接口为受控制台鉴权保护的 `POST /api/accounts`，例如 `{"name":"账号 A","credentials":{"token":{...}}}`，省略 `credentials` 则创建待登录账号。

- 每个账号拥有独立的用户目录、OAuth 文件、CLI 状态、缓存、日志和对话历史。账号自己的 `state.json` 保存固定 UUID，并写入其 CLI 的 `installation_id`。新增 B 会生成与 A 不同的标识；重启、请求重试及进程回收不会重新生成。
- 同一客户端 API Key（未带 Key 时为匿名客户端）持续绑定同一账号，模型变化、会话超时及代理重启均保留绑定。新客户端默认使用第一个可用账号，不按请求轮换。
- 只有上游明确报告额度耗尽，或新鲜的额度查询明确返回相关模型组剩余为 **0**，才允许切换。低余额、未知额度、普通 429 / 频率限制、超时、认证失败及网络错误不会触发切号；已绑定账号的凭据缺失也会返回错误。
- 额度耗尽时，若尚未交付内容且未执行服务端工具，本次请求自动用下一账号重放完整历史。已输出部分内容或执行工具则终止本轮，下一次请求使用新账号，避免重复输出或重复执行。所有账号耗尽时返回额度错误。
- 切到 B 后，即使 A 恢复额度也继续使用 B。可在账号卡片中刷新额度；没有上游重置时间的耗尽记录会保留到查询确认恢复。

所有管理数据保存在 `DATA_DIR` 下：`accounts.json` 保存账号索引、客户端绑定和耗尽状态；`accounts/<id>/state.json` 保存固定身份；`accounts/<id>/home/` 保存各账号的原生 CLI 数据。Docker 的 `agy-data` 卷必须整体持久化，备份也需包含整个数据目录。未创建隔离账号时兼容原来的本机登录方式。账户索引由单个代理进程管理，多个实例须使用各自的数据目录。

这里固定的是 CLI 安装标识。参考项目的 IDE 指纹字段、CLI 原生 `state.json` 与代理账号 `state.json` 的区别，以及已验证的凭据隔离行为，见 [实现与验证说明](docs/account-isolation.md)。

## 配置（环境变量，前缀 `AGY_PROXY_`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BIN` | `agy` | agy 可执行文件（可含完整命令行，如 `python fake.py`） |
| `HOST` / `PORT` | `127.0.0.1` / `8317` | 监听地址 |
| `PASSWORD` | 空 | **控制台密码**，保护 `/`、`/login`、`/update`、`/dashboard`、`/api/*`。为空则控制台无鉴权（仅适合本机） |
| `API_KEYS` | 空 | 逗号分隔的**固定** API 密钥，供客户端调 `/v1/*`；也兼作控制台登录凭证。常规做法是留空、改在控制台里创建 |
| `DATA_DIR` | `~/.agy-proxy` | API Key、用量记录及隔离账号、固定身份、账号绑定所在目录，需整体持久化 |
| `DEFAULT_MODEL` | 空 | 未知模型兜底映射到的 slug |
| `MODEL_MAP` | 空 | JSON，如 `{"gemini-3-pro-preview":"gemini-3-pro-high"}` |
| `MAX_SESSIONS` | `8` | agy 进程数上限；回收最久未用的空闲进程，全部忙时返回 429 |
| `SESSION_TTL` | `1800` | 会话及预热进程空闲回收秒数，包含服务端自动关联的会话 |
| `TURN_TIMEOUT` | `600` | 单轮总超时秒数 |
| `IDLE_TIMEOUT` | `0` | stdout 静默超时，0 禁用；CLI 思考可能不输出 stdout，通常应保持禁用 |
| `STREAM_HEARTBEAT` | `10` | 流式 HTTP 等待期间的 SSE 心跳间隔（秒） |
| `MODELS_TTL` | `300` | `agy models` 结果缓存秒数 |
| `EFFORT` | 空 | OpenAI 请求未指定 `reasoning_effort` 时使用的默认思考级别 |
| `AGENT` | 空 | 进程级默认 `--agent` |
| `SKIP_PERMISSIONS` | `false` | 传 `--dangerously-skip-permissions`（允许 agy 执行工具，谨慎） |
| `SANDBOX` | `false` | 传 `--sandbox` |
| `HTTP_PROXY` | 空 | agy 子进程的出站代理（如 `127.0.0.1:7890`，自动补 `http://`），注入为 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 及小写变体。**网络需代理才能访问 Google 时必设**，否则 agy 会报 "Please sign in" 这类误导性错误 |
| `CWD` | 空 | agy 子进程工作目录。建议指向一个空目录，避免代理运行目录下的 `AGENTS.md`/`GEMINI.md` 等规则文件被注入上下文 |
| `PREWARM_MODELS` | 空 | 逗号分隔的模型 slug（如 `gemini-3.1-pro-high`）。为每个模型预热一个空闲会话，首个请求可省去约 6 秒的 agy 进程冷启动（实测 41.4s → 23.7s，剩余为模型思考时间） |

模型解析顺序：`MODEL_MAP` 精确匹配 → `agy models` 列表精确匹配 → 子串模糊匹配 → `DEFAULT_MODEL` → 原样透传（由 agy 报错）。
Gemini 的 `effort` 来自 `generationConfig.thinkingConfig.thinkingLevel`（low/medium/high）。OpenAI 使用下方的模型名称与 `reasoning_effort` 组合解析规则。

## 使用示例

### Gemini 格式

```bash
# 非流式
curl -X POST "http://127.0.0.1:8317/v1beta/models/gemini-3-pro-high:generateContent" \
  -H "content-type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"用一句话介绍你自己"}]}]}'

# 流式 (SSE)
curl -N -X POST "http://127.0.0.1:8317/v1beta/models/gemini-3-pro-high:streamGenerateContent?alt=sse" \
  -H "content-type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"讲个笑话"}]}]}'
```

Gemini SDK 接入：`client = genai.Client(api_key="任意", http_options={"base_url": "http://127.0.0.1:8317"})`

### OpenAI 格式

```bash
curl -X POST http://127.0.0.1:8317/v1/chat/completions \
  -H "content-type: application/json" \
  -d '{"model":"gemini-3-pro-high","messages":[{"role":"user","content":"你好"}]}'
```

openai SDK 接入：`OpenAI(base_url="http://127.0.0.1:8317/v1", api_key="任意或配置的密钥")`

### 模型名称 + 思考级别

Chat Completions 支持用不带档位后缀的模型名称，配合标准字段 `reasoning_effort` 选择思考级别：

```json
{
  "model": "gemini-3.8-flash",
  "reasoning_effort": "high",
  "messages": [{"role": "user", "content": "分析这段代码的边界条件"}],
  "stream": false
}
```

该请求选择 `gemini-3.8-flash-high`，并传入 CLI 的 `--effort high`。`stream: true`、客户端工具调用、JSON/JSON Schema 模式使用相同规则。`/v1/models` 同时列出基础名称和原有完整模型 ID，响应中的 `model` 保持客户端请求的名称。

控制台「模型列表」按基础模型分组，可统一启用或禁用整组变体。例如关闭 `gemini-3.8-flash` 后，其 low/medium/high 等变体也会禁用，不再出现在 `/v1/models` 和 Gemini 模型同步列表中；通过旧 ID、别名或默认模型直接调用会返回 `404`。设置保存在数据目录的 `data.json`，重启后仍生效。概览中的「总缓存」显示累计缓存命中 Tokens。

优先级：请求的 `reasoning_effort` → `AGY_PROXY_EFFORT` → 模型名称自带的档位/原有默认选择。例如 `model: "gemini-3.8-flash-low"` 配合 `reasoning_effort: "high"` 会选择同系列的 High 模型；未设置思考参数与默认值时，原有完整模型 ID 保持不变。`null` 等同于未指定。

支持的档位以 `agy models` 的实时模型变体为准；例如当前 Flash 有 low/medium/high，而 Gemini 3.1 Pro 只有 low/high，给后者传 medium 会返回 `400`，并在 `error.param: "reasoning_effort"` 和错误信息中列出可用值。xhigh/max 仅在该模型实际提供对应变体时使用；`auto` 和没有已知变体的自定义 `MODEL_MAP` 目标直接使用 CLI 的 `--effort` 参数，由 CLI 校验。

`minimal` 沿用代理既有兼容行为，映射为 low；CLI 不支持关闭思考，因此 `none` 返回 `400`，不会假装关闭。无效类型、空字符串和未知档位同样返回 `400`。模型目录查询失败时不会猜测模型档位。

```python
result = client.chat.completions.create(
    model="gemini-3.8-flash",
    reasoning_effort="high",
    messages=[{"role": "user", "content": "分析这段代码的边界条件"}],
)
```

字段规范见 [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)，CLI 参数见 [Antigravity headless mode](https://antigravity.google/docs/cli/headless/)。

支持 `response_format: {"type":"json_object"}` 和 `{"type":"json_schema","json_schema":{"name":"result","strict":true,"schema":{...}}}`。代理将 Schema 交给 agy 的 `--json-schema`，并在返回前校验 JSON 和 Schema；不符合要求的上游结果返回错误，不会作为成功响应透传。结构化请求使用独立会话，Schema 根类型须为 object，引用仅限本地 `$ref`；当前不能与客户端工具模式组合。结构化流式响应会先缓冲和校验完整结果，再发送标准 SSE 内容、usage（按需）及 `[DONE]`，因此没有逐 token 的首字延迟优势。

模型列表可用时，未知模型返回 OpenAI 格式的 `404 / model_not_found`，不会静默回退到默认模型；仍支持显式 `default`、`auto`、模型简称及 `MODEL_MAP` 中配置的自定义别名。常用采样参数被接受不代表 agy CLI 实际支持这些参数；兼容性测试分数不能代替参数语义验证。

兼容路径变体：`:generateContent` 与 `/generateContent`、`/v1beta/` 与 `/v1/`、带或不带 `models/` 前缀均可。

### Pi coding agent 与客户端工具

带 `tools`、`tool_choice` 或工具历史的 OpenAI 请求自动进入**客户端工具模式**（工具参数对象协议已实测 agy 1.2.16；下方 Pi 配置适用于 0.87.1）：

1. 代理为 agy 创建独立临时工作目录，选择仅允许 `finish` 的自定义主 Agent；`PreToolUse` hook 拒绝其余服务端工具。`finish` 只负责提交结构化结果，必须保留。
2. 把客户端工具定义和消息交给 agy，使用 `--json-schema` 获取调用请求。内部工具参数直接使用 JSON 对象，校验工具名、参数 schema 和 `tool_choice` 后，由代理序列化成 OpenAI `tool_calls[].function.arguments` 字符串，结束原因为 `tool_calls`。这样避免让模型在 JSON 字符串里再次编码参数，减少文件路径、代码中的引号与换行造成的解析错误。
3. **Pi 在运行 Pi 的机器上执行** `read`、`write`、`edit`、shell 等工具，然后回传 `role: tool` 和 `tool_call_id`。代理在独立上游进程中重放完整客户端消息，保留调用参数和所有工具结果，支持多轮及多个工具结果。

这是一层结构化输出适配，不是 agy 原生 function calling 直通。不会把 agy 的 `tool_info` 执行记录转交 Pi 再执行；发现意外服务端工具调用会丢弃响应并返回错误。CLI 的 `init.tools` 仍可能列出内置工具，不能用这份列表判断实际允许执行的工具。临时目录与 hook 是 CLI 级限制，不是操作系统安全沙箱；不会修改全局 agy 设置，且客户端工具模式不使用 `AGY_PROXY_AGENT` / `SKIP_PERMISSIONS`。

Pi 的 `%USERPROFILE%\.pi\agent\models.json`（Linux/macOS 为 `~/.pi/agent/models.json`）使用独立 provider，模型 ID 使用 `/v1/models` 返回的 agy slug：

```json
{
  "providers": {
    "agy-proxy": {
      "baseUrl": "http://你的服务器:8317/v1",
      "apiKey": "控制台创建的API密钥",
      "api": "openai-completions",
      "authHeader": true,
      "models": [
        { "id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash High", "reasoning": false, "input": ["text"] }
      ]
    }
  }
}
```

```bash
pi --provider agy-proxy --model gemini-3.8-flash-high --thinking off
```

启用工具时不要加之前用于连通测试的 `--no-tools`。不要覆盖内置 `openrouter` provider，也不要使用 `google/gemini-3.8-flash` 作为模型 ID。

支持 `tool_choice: auto/none/required/指定函数` 和 `parallel_tool_calls: false`。工具模式的 SSE 会等完整结构化结果校验完成再发送内容与调用，参数不逐字符流出。当前仅支持文本、function 类型工具和 `n=1`，参数 schema 支持本地 `$ref`，不加载外部引用；不支持与 `response_format: json_object/json_schema` 叠加。`tools: []` 可显式使用无客户端工具的受限模式。普通不带工具字段或历史的请求保留原有 agy 行为。

### Codex：Responses API

`POST /v1/responses` 支持文本、多轮完整历史、`instructions`、function/custom 工具、namespace 工具、`reasoning.effort`、无工具的 JSON Schema 输出，以及带序号的 Responses SSE 生命周期。客户端负责执行工具，回传 `function_call_output` / `custom_tool_call_output`。

Codex 的 `config.toml` 示例（密钥通过 `OPENAI_API_KEY` 环境变量提供）：

```toml
model = "gemini-3.8-flash"
model_provider = "agy"
model_reasoning_effort = "low"
model_context_window = 200000
web_search = "disabled"

[model_providers.agy]
name = "AGY Proxy"
base_url = "http://你的服务器:8317/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
supports_websockets = false
```

`store: false` 使用完整客户端历史即可，不保存响应。默认 `store: true` 支持 `previous_response_id`、`GET /v1/responses/{id}`、`DELETE /v1/responses/{id}`。缓存按 API Key 隔离，当前是**单进程内存缓存**：TTL 一小时、最多 256 条、总量 32 MiB，重启或超限淘汰后返回 404。每次续接仍创建私有上游进程；同一响应可以显式分叉，分支不会共享可变历史。上一轮 `instructions` 不自动继承。

### Claude Code：Anthropic Messages API

`POST /v1/messages` 支持 `x-api-key` / Bearer 鉴权、system 文本块、`tool_use` / `tool_result`、并行工具、`tool_choice`、`stop_sequences` 和 Messages SSE，也兼容 Claude Code 2.1.295 在消息列表中附加的 system 环境信息。`output_config.effort` 复用模型思考档位解析；`thinking.type=enabled` 默认选择 high，显式 effort 优先。不生成虚假的 thinking/signature 内容。

```bash
export ANTHROPIC_BASE_URL="http://你的服务器:8317"
export ANTHROPIC_API_KEY="控制台创建的API密钥"
export ANTHROPIC_MODEL="claude-sonnet-5-5"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$ANTHROPIC_MODEL"
export CLAUDE_CODE_SUBAGENT_MODEL="$ANTHROPIC_MODEL"
export ENABLE_TOOL_SEARCH=false
claude --effort low
```

模型 ID 必须取自 `/v1/models`，也可以让两个客户端使用同一个 Gemini 模型。Messages 的 Base URL 不包含 `/v1`，Codex 的 Base URL 包含 `/v1`。

两个新接口始终使用受限的客户端工具执行模式，含无工具请求。SSE 在等待期间发送心跳，工具结果校验通过后发送内容；目前不是逐 token 输出。断开连接会取消上游进程并释放工作目录。

兼容范围：支持文本、用户图片输入和客户端工具；不支持音视频、图片生成、工具返回图片、内置服务端搜索、后台任务、WebSocket、Responses conversation 资源或外部加密 reasoning。custom 工具的 grammar 会交给模型指导生成，代理仅校验返回的是字符串，不实现 grammar 解析器。`cache_control` 不建立 Anthropic prompt cache；缓存创建/读取计数为 0。`POST /v1/messages/count_tokens` 提供明确标记为 `X-Agy-Token-Count: estimate` 的近似值，图片按像素粗估，不把 Base64 长度当 Token，也不用于 dashboard 记账。agy CLI 尚不暴露 token 上限或采样参数，`max_tokens` / `max_output_tokens` / thinking budget 不能作为硬性限额，`temperature` / `top_p` 不透传；实际 usage 来自上游。JSON Schema 输出暂不与客户端工具调用叠加。

### MCP：stdio / Streamable HTTP

Pi、Codex、Claude Code、OpenCode 可以在客户端连接 MCP，通过上面的 Chat Completions / Responses / Messages 接口完成工具调用。[examples/mcp](examples/mcp) 提供 Exa Streamable HTTP 和 Windows filesystem stdio 的四套配置；真实客户端验收脚本位于 [tests/mcp_smoke.py](tests/mcp_smoke.py)。

### 图片输入

三个接口共用图片校验、下载和隔离执行逻辑，支持 PNG、JPEG、WebP、静态 GIF，接受 Base64 或公网 HTTP(S) 图片。图片可与客户端工具调用、无工具的 JSON Schema 输出及历史续接配合使用。单张图片最多 10 MiB、1600 万像素，每次请求最多 8 张、合计 20 MiB／3200 万像素。真实 API 和客户端验收脚本分别为 [tests/vision_api_smoke.py](tests/vision_api_smoke.py) 和 [tests/vision_smoke.py](tests/vision_smoke.py)。

### 客户端 Skills

Pi、Codex、Claude Code、OpenCode 在客户端发现和加载 `SKILL.md`，代理保留技能指令、引用内容和工具调用；脚本在客户端执行，无需安装到代理服务器。支持自动选择以及客户端各自的显式调用方式。验收脚本 [tests/skills_smoke.py](tests/skills_smoke.py) 覆盖技能加载、引用读取、脚本执行及独立会话数据检查，使用 `uv run python tests/skills_smoke.py` 运行。

## 鉴权（公网部署必读）

两把**互不通用**的凭据，职责分开：

| 凭据 | 用途 | 来源 |
|---|---|---|
| **控制台密码** `AGY_PROXY_PASSWORD` | 登录 `/signin` → 使用 `/`（控制台）、`/login`、`/update`、`/dashboard`、`/api/*` | 环境变量。**控制台自身的凭证，不能给客户端当 API Key 用** |
| **上游 API Key** | 客户端调用 `/v1/*`、`/v1beta/*` | 控制台「API Keys」里创建（可随时吊销），或 `AGY_PROXY_API_KEYS` 里配置的固定密钥 |

- **网页登录**：未携带有效凭证访问面板页面会跳转到 **`/signin`**，填控制台密码即可；通过后服务端种下 7 天有效的 HttpOnly Cookie。退出登录访问 `/signout`
- **API 调用**：接受 `Authorization: Bearer <key>`、`Basic`（`curl -u :key`）、`x-api-key`、`x-goog-api-key`、`?key=`、以及浏览器 Cookie 任一形式
- 兼容：未设置 `AGY_PROXY_PASSWORD` 时，`AGY_PROXY_API_KEYS` 里的密钥仍可登录控制台（升级旧部署不用改 `.env`；启动日志会打 WARNING 提示补设 PASSWORD）
- `/health`：仅返回状态/版本/是否已设置鉴权/是否已登录，不泄露部署细节；`/docs`、`/openapi.json` 已关闭
- 未设置任何凭据时服务完全开放（`/signin` 会提示"未设置鉴权"），启动日志会打 WARNING——仅适合本机使用

## 控制台

打开 `http://<IP>:<PORT>/`，四个区块：

1. **用量** — 累计请求数、输入/输出/思考/缓存命中 token（**计数直接取自 agy 每次 result 事件的 `usage` 字段**，代理不做估算）
2. **可用模型** — `agy models` 的实时列表（含人类可读标签），并叠加每个模型的请求数与 token 消耗、相对用量条
3. **API Keys** — 创建（明文只在创建时展示一次）、改名、删除。删除后客户端立即收到 401；每把 key 单独统计请求数与 token
4. **最近请求** — 时间、模型、使用哪把 key、状态（成功/错误码/中断）、耗时、token

数据写在 `AGY_PROXY_DATA_DIR`（默认 `~/.agy-proxy/data.json`），改动静默去抖落盘、启动时读回；Docker 部署已挂 volume `agy-data`。
页面每 5 秒自动刷新。管理接口：`GET /dashboard`、`GET /api/models`、`POST|PATCH|DELETE /api/keys[/{id}]`（均需控制台凭证）。
样式来自一份编译好的 `app/static/app.css`，四个页面共用（见「前端样式」）。

**账号详情**（`/login`）按账号 Card 展示身份、套餐、上游连接状态和检查时间。点击卡片的 **查看 Usage**，弹窗显示 Gemini、Claude/GPT 等模型组的每周与五小时剩余额度、刷新倒计时，支持手动刷新；登录操作也在卡片弹窗内完成。

额度来自 CLI `/usage` 使用的 Cloud Code 查询接口，默认缓存 60 秒，失败缓存 10 秒，并发刷新合并；不会用本地 Token 计数估算额度。接口格式变化或查询失败会明确提示，未知额度不会显示成 0%。当前后端展示当前服务使用的一个 CLI 账号。账号接口为 `GET /api/accounts`、`GET /api/accounts/current/usage`，可传 `?refresh=true`，均需控制台凭证。

Docker 中支持当前 CLI 的 `~/.gemini/antigravity-cli/antigravity-oauth-token` 凭据；OAuth 令牌只在内存刷新，不覆盖 CLI 凭据文件。创建 API Key 的复制按钮支持 HTTPS Clipboard API 和 HTTP 下的复制兼容路径；浏览器拒绝全部复制方式时仍可手动选中完整密钥。

## 已知限制

- **仍经过 agy 的 Agent 层**：客户端工具模式使用自定义主 Agent 限制工具；系统上下文、模型行为与原生模型 API 不完全相同。
- **Gemini 原生接口**的图片 `inlineData` 与 function calling 暂未实现；图片请使用上述三个 `/v1` 接口。工具/MCP 返回图片暂不支持；`temperature` 等采样参数仍不透传。
- 普通纯文本且不带工具字段或历史的 Chat 请求仍可能让 agy 使用其服务端工具（受自身权限策略约束）；需要限制时显式发送 `tools: []`。带图片的请求自动使用受限执行上下文，只允许读取本次图片附件和提交结果。
- 每次请求都使用干净的上游进程，未命中未使用的预热进程时有秒级冷启动开销
- 需遵守 Antigravity 服务条款与账号额度，本代理不做额外限流（按 key 只做统计，不做配额；可用 `MAX_SESSIONS` 约束并发）
- 控制台用量只保留**最近 500 条**请求明细（更早的只进总量与按模型聚合），且首次启动前的历史不可追溯

## 测试

```bash
uv run pytest    # 使用 tests/fake_agy.py 模拟 agy 协议，无需真实登录
```

## 前端（React + shadcn/ui）

控制台、登录、账号连接和 CLI 更新四个页面使用 **React 19 + TypeScript + Vite + Tailwind CSS v4**。通过官方 shadcn CLI 安装的 New York / Radix 组件源码位于 `src/components/ui/`，包括 Button、Card、Table、Tabs、Field、Dialog、AlertDialog 和 Sonner；配置见 `components.json`。

```bash
npm ci
npm run build          # 类型检查并编译到 app/static/
npm run dev            # Vite 开发服务器（数据接口转发到本机 8317）
```

样式及主题统一在 `src/index.css`，应用入口为 `src/main.tsx`。`npm run build:css` 兼容旧命令，现在会重新构建整个前端。登录 POST 请求在开发服务器中通过代理转发；生产环境全部由 FastAPI 同源提供。

`app/static/index.html`、`app/static/app.css`、`app/static/assets/` 是需要一起提交的编译产物。生产运行和 Docker 镜像无需 Node、CDN 或外部字体。修改前端后先运行 `npm run build`，再刷新页面；修改 Python 路由后需要重启服务。

可运行浏览器回归检查（使用独立临时数据与模拟 agy，不操作真实账号）：

```bash
uv run --with playwright python tests/browser_smoke.py
```

该检查默认使用本机 Edge；其他环境可设置 `UI_BROWSER_CHANNEL=chromium` 并先安装 Playwright Chromium。

## Docker 部署（含远程登录桥）

镜像内置 agy CLI（官方 install.sh 安装）。部署需要两个数据卷：`agy-gemini`（`/root/.gemini`）保存 AGY 登录凭据，`agy-data`（`/root/.agy-proxy`）保存控制台创建的 API 密钥与用量记录。只挂载前者，容器重建后仍能登录 AGY，但密钥和用量可能丢失。镜像已发布：`docker.io/tumuer/agy-proxy`。

账号详情刷新过期 OAuth 凭据时，需要通过本地 `.env` 配置 `AGY_PROXY_OAUTH_CLIENT_ID` 和 `AGY_PROXY_OAUTH_CLIENT_SECRET`，其值必须匹配签发当前凭据的 CLI OAuth 客户端。未配置时仍可查询未过期凭据，过期后页面会提示配置或重新授权。真实值不应提交到仓库或写入镜像。

### 一条命令启动（docker compose + .env）

服务器上只需两个文件（都在本仓库里）：

```bash
mkdir agy-proxy && cd agy-proxy
# 放入 docker-compose.yml，然后：
cp .env.example .env   # 或从仓库拷贝 .env.example
vim .env               # 至少填 AGY_PROXY_PASSWORD（openssl rand -hex 32 生成）

docker compose up -d
docker compose logs -f   # 确认启动正常
```

浏览器打开 `http://<IP>:<PORT>/` → 跳转到 `/signin` → 填控制台密码 → 进入控制台。agy 未登录时先去 `http://<IP>:<PORT>/login` 完成授权。

### 升级与数据持久化

`docker compose pull` 只拉取镜像，**不会更新服务器上的 `docker-compose.yml`**。升级前先确认服务器配置包含以下两个挂载及顶层卷声明（合并到已有配置，保留其他设置）：

```yaml
services:
  agy-proxy:
    volumes:
      - agy-gemini:/root/.gemini
      - agy-data:/root/.agy-proxy

volumes:
  agy-gemini:
  agy-data:
```

检查正在运行的容器，而不仅是配置文件：

```bash
docker inspect agy-proxy --format '{{json .Mounts}}'
```

输出应包含 `/root/.gemini` 和 `/root/.agy-proxy` 两个目标目录。Compose 默认给卷名加项目名前缀，例如 `agy-proxy_agy-data`；升级时保持原目录、项目名和卷名。`docker run` 示例中的 `agy-data` 与 Compose 的 `agy-proxy_agy-data` 是不同的卷，切换部署方式时需显式复用原卷。

若原部署缺少数据挂载，先从当前容器备份 `/root/.agy-proxy/data.json`，再补齐挂载；直接重建会丢弃容器内尚未持久化的数据。备份文件包含完整密钥，应保存在受限目录，例如：

```bash
agy_backup_dir=$(mktemp -d /tmp/agy-proxy-backup.XXXXXX)
docker cp agy-proxy:/root/.agy-proxy/data.json "$agy_backup_dir/data.json"
```

确认备份成功后再迁移，并在服务停止时将备份恢复到新数据卷的 `data.json`。已有正确挂载的部署，修改镜像 tag（如 `tumuer/agy-proxy:v0.0.14`）后执行：

```bash
docker compose pull
docker compose up -d
```

升级不需要 `docker compose down -v`，该命令会删除 Compose 管理的数据卷。修改 `.env` 后也要执行 `docker compose up -d`，仅 `restart` 不会加载新的容器环境变量。

**升级后密钥与用量同时为空：** 检查实际挂载和 `data.json` 是否存在，再查看启动日志中的 `loaded ... api key(s)`、`could not read` 或 `could not persist`。当前后端在读取失败时会使用空状态，因此空列表本身不能证明数据文件已删除。若有可恢复的文件，先备份再处理，避免创建密钥或请求记录时写回空状态。

若此前未挂载数据目录，旧容器已删除且没有备份，则无法通过重新拉取镜像恢复原用量记录。若 Pi 等客户端配置还保存着原密钥，可以将其加入服务器 `.env` 的 `AGY_PROXY_API_KEYS`（多个密钥用逗号分隔），并确认服务的 `environment` 包含 `AGY_PROXY_API_KEYS: ${AGY_PROXY_API_KEYS:-}`，随后执行 `docker compose up -d` 恢复原客户端的鉴权。固定密钥不会出现在控制台密钥列表中，也不恢复原用量；补齐数据挂载后，可以重新在控制台创建并管理密钥。

### 手动 docker run（等价）

```bash
docker run -d --name agy-proxy -p 8317:8317 \
  -v agy-gemini:/root/.gemini \
  -v agy-data:/root/.agy-proxy \
  -e AGY_PROXY_HTTP_PROXY=http://host.docker.internal:7890 \
  -e AGY_PROXY_PASSWORD=你的控制台密码 \
  --add-host=host.docker.internal:host-gateway \
  tumuer/agy-proxy:v0.0.14
```

### 容器内 agy 的 OAuth 登录（浏览器在你自己的电脑上完成）

容器没有浏览器，代理内置了登录桥：用 PTY 驱动 agy 的登录界面，把 Google 授权链接透出到网页上。

1. 打开 `http://<部署机IP>:8317/login`，点 **开始连接**。网页会显示 CLI 当前画面；遇到登录方式、授权确认等菜单时，用 **↑ / ↓** 选择，核对选项后点 **回车确认**。需要勾选时用 **空格选择**，也支持 Tab、Esc 和 Y/N。
2. 页面出现 **打开 Google 授权页** 按钮 → 在你自己的电脑浏览器里打开并完成授权
3. 授权后浏览器会跳转到 `https://antigravity.google/oauth-callback?...`——从该页面（或地址栏的 `code=` 参数）复制授权码
4. 粘回 `/login` 页面的输入框并提交；凭据保存后，如 CLI 还有确认提示可继续操作，最后点 **完成并关闭**（凭据写入 volume）

说明：CLI 确认由用户手动操作，登录桥不会自动同意。可点击终端画面后使用键盘；浏览器 Tab 用于移出画面，发送 Tab 到 CLI 请使用按钮。点击「取消本次登录」会结束当前进程，之后可以重新连接。授权码无效时按 CLI 提示手动回车并重新提交；整次登录有 15 分钟超时；登录进程只在登录期间存在。`/login` 与控制台同权限（需登录后才能访问）。

### 容器内更新 agy CLI（无需重建镜像）

控制台页头的 **agy vX.Y.Z** 徽章即当前容器内 agy 版本，点进去（或直接访问 `/update`）：

1. 页面显示当前版本，点 **⟳ 检查并更新**
2. 代理执行官方 `agy update`（检查 → 下载 → 校验 → 安装），进度实时显示
3. 完成后新会话自动使用新版本；已是最新会直接提示，无需重启容器

说明：更新前会先结束**空闲** agy 会话以释放旧版本二进制（进行中的请求不受影响，用完旧版后自然重建）；容器重建（`docker compose up -d` 换镜像）后 agy 会回到镜像内置版本，属预期。`AGY_PROXY_HTTP_PROXY` 同样作用于更新子进程。API：`GET /update/status`、`POST /update/start`（与控制台相同的鉴权约定）。

## 延迟分析（profiling）

每个请求都会输出分相耗时日志，流式响应末尾附带 SSE 注释行 `: agy-profile ...`（客户端会忽略），非流式响应带 `X-Agy-Profile` 响应头：

```
turn profile: model=gemini-3.1-pro-high new_session=1 models_lookup=0ms
acquire=16ms agy_boot=6312ms think=28094ms stream=7016ms total=41438ms
```

| 阶段 | 含义 | 优化手段 |
|---|---|---|
| `models_lookup` | `agy models` 查询（缓存命中应为 0） | 过期时自动"先回旧值、后台刷新" |
| `acquire` | 会话匹配/新建 | — |
| `agy_boot` | agy 进程冷启动（新会话才有，约 6s） | `PREWARM_MODELS` 预热 → 归零 |
| `think` | 模型思考（首字符前） | 换 flash 模型 / `--effort` 降档 |
| `stream` | 流式输出耗时 | 取决于模型输出速度 |

实测 `gemini-3.1-pro-high` 冷启动 41.4s 中：agy 启动 6.3s（可预热消除）、思考 28s、输出 7s；你看到的 65s 属于模型思考时长的正常波动（同 prompt 复测 18~50s 不等），代理自身开销 < 20ms。

## 项目结构

```
app/
├── config.py     # 环境变量配置
├── agy.py        # AgySession（常驻进程/NDJSON 读写）+ SessionManager + 模型发现
├── runner.py     # 回合执行器：会话续接、增量文本、seed 拍平
├── session_context.py # 通用 agent 会话标识、凭据隔离和历史指纹
├── json_codec.py # orjson 编解码与 HTTP JSON 响应
├── streaming.py  # SSE 保活与断连清理
├── store.py      # 控制台持久化：上游 API Key + agy 用量记录（单个 JSON）
├── auth.py       # 控制台密码 / 上游 API Key 两套鉴权 + /signin
├── dashboard.py  # 控制台数据与 API Key 管理接口
├── gemini_api.py # Gemini 路由与格式转换
├── openai_api.py # OpenAI 路由与格式转换
├── external_tools.py # 客户端工具协议：schema 校验、工具调用/结果与请求隔离
├── images.py     # 三协议图片校验、下载、历史快照与附件映射
├── tool_guard.py # PreToolUse hook：finish 与本次图片精确路径白名单
├── login.py      # 容器内 agy OAuth 登录桥（PTY + /login 页面）
├── update.py     # 容器内 agy 自更新桥（agy update + /update 页面）
└── static/       # 控制台页面 + 编译好的 app.css（构建产物，已入库）
src/              # React 页面、shadcn/ui 组件与主题
components.json   # shadcn New York / Radix 配置
vite.config.ts    # Vite 构建与开发代理
package.json      # 前端构建链（仅 devDependencies，运行时不需要 Node）
tests/            # fake_agy 协议模拟器 + 端到端测试
```
