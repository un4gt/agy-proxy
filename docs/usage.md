# 客户端接入与 API 兼容

[返回 README](../README.md) · [环境变量](configuration.md)

四种推理 API 统一使用受限客户端模式。未声明工具时只能回答；声明的工具由客户端执行。Chat/Gemini 的普通文本 SSE 也先发送开始标记／心跳，等待完整结果校验后再交付正文，不再转发 CLI 的逐 token 输出。协议能力、恢复行为及升级变化见[统一协议兼容层](unified-client-protocols.md)。

先在控制台创建 API Key，并从 `GET /v1/models` 获取实际可用的模型 ID。示例中的模型名需按你的账号替换；curl 示例使用 Bash：

```bash
export AGY_API_KEY="控制台创建的API密钥"
```

## Token 统计口径

AGY CLI 的 `input_tokens` 和 `total_tokens` 不含 `cache_read_tokens`，
`output_tokens` 已含 `thinking_tokens`。代理内部及持久化记录保留这套原始口径。
OpenAI Chat / Responses 和 Gemini 的输入量、总量会加上缓存输入；缓存详情是输入量的子集。
Anthropic Messages 的 `input_tokens` 则继续表示未缓存输入，缓存量单独返回。
例如 CLI 返回输入 3,152、缓存 14,126、输出 99 时，OpenAI 应返回
`prompt_tokens=17278`、`cached_tokens=14126`、`total_tokens=17377`。
旧版本客户端已保存的统计不会因服务器升级自动重算。

## Gemini 格式

```bash
# 非流式
curl -X POST "http://127.0.0.1:8317/v1beta/models/gemini-3-pro-high:generateContent" \
  -H "Authorization: Bearer $AGY_API_KEY" \
  -H "content-type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"用一句话介绍你自己"}]}]}'

# 流式 (SSE)
curl -N -X POST "http://127.0.0.1:8317/v1beta/models/gemini-3-pro-high:streamGenerateContent?alt=sse" \
  -H "Authorization: Bearer $AGY_API_KEY" \
  -H "content-type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"讲个笑话"}]}]}'
```

Gemini 路径兼容 `:generateContent` 与 `/generateContent`、`/v1beta/` 与 `/v1/`、带或不带 `models/` 前缀。

Gemini SDK 接入：`client = genai.Client(api_key="控制台创建的API密钥", http_options={"base_url": "http://127.0.0.1:8317"})`

## OpenAI 格式

```bash
curl -X POST http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer $AGY_API_KEY" \
  -H "content-type: application/json" \
  -d '{"model":"gemini-3-pro-high","messages":[{"role":"user","content":"你好"}]}'
```

openai SDK 接入：`client = OpenAI(base_url="http://127.0.0.1:8317/v1", api_key="控制台创建的API密钥")`

## 模型名称 + 思考级别

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

支持 `response_format: {"type":"json_object"}` 和 `{"type":"json_schema","json_schema":{"name":"result","strict":true,"schema":{...}}}`。代理将 Schema 交给 agy 的 `--json-schema`，并在返回前校验 JSON 和 Schema；不符合要求的上游结果返回错误，不会作为成功响应透传。结构化请求按客户端会话隔离，连续历史且 Schema 等参数一致时可续用原进程；Schema 根类型须为 object，引用仅限本地 `$ref`；当前不能与客户端工具模式组合。结构化流式响应会先缓冲和校验完整结果，再发送标准 SSE 内容、usage（按需）及 `[DONE]`，因此没有逐 token 的首字延迟优势。

模型列表可用时，未知模型返回 OpenAI 格式的 `404 / model_not_found`，不会静默回退到默认模型；仍支持显式 `default`、`auto`、模型简称及 `MODEL_MAP` 中配置的自定义别名。常用采样参数被接受不代表 agy CLI 实际支持这些参数；兼容性测试分数不能代替参数语义验证。

## Pi coding agent 与客户端工具

所有 OpenAI 请求均进入**受限客户端模式**；声明 `tools` 时启用客户端工具桥接（下方 Pi 配置适用于 0.87.1）：

1. 代理为 agy 创建独立临时工作目录，选择仅允许 `finish` 的自定义主 Agent；`PreToolUse` hook 拒绝其余服务端工具。`finish` 只负责提交结构化结果，必须保留。
2. 把客户端工具定义和消息交给 agy，使用 `--json-schema` 获取调用请求。内部工具参数直接使用 JSON 对象，校验工具名、参数 schema 和 `tool_choice` 后，由代理序列化成 OpenAI `tool_calls[].function.arguments` 字符串，结束原因为 `tool_calls`。这样避免让模型在 JSON 字符串里再次编码参数，减少文件路径、代码中的引号与换行造成的解析错误。
3. **Pi 在运行 Pi 的机器上执行** `read`、`write`、`edit`、shell 等工具，然后回传 `role: tool` 和 `tool_call_id`。代理核对客户端历史后，向该会话的独立上游进程发送增量；需要新进程时重放完整历史，保留调用参数和所有工具结果。

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

支持 `tool_choice: auto/none/required/指定函数` 和 `parallel_tool_calls: false`。工具模式的 SSE 会等完整结构化结果校验完成再发送内容与调用，参数不逐字符流出。支持文本、用户图片输入和 function 类型工具，`n` 仅支持 `1`，参数 schema 支持本地 `$ref`，不加载外部引用；不支持与 `response_format: json_object/json_schema` 叠加。`tools: []` 可显式使用无客户端工具的受限模式。普通不带工具字段或历史的请求保留原有 agy 行为。

## Codex：Responses API

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

`store: false` 使用完整客户端历史即可，不保存响应。默认 `store: true` 支持 `previous_response_id`、`GET /v1/responses/{id}`、`DELETE /v1/responses/{id}`。缓存按 API Key 隔离，当前是**单进程内存缓存**：TTL 一小时、最多 256 条、总量 32 MiB，重启或超限淘汰后返回 404。连续历史可复用该会话的独立上游进程；同一响应可以显式分叉，分支不会共享可变历史。上一轮 `instructions` 不自动继承。

## Claude Code：Anthropic Messages API

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

## MCP：stdio / Streamable HTTP

Pi、Codex、Claude Code、OpenCode 可以在客户端连接 MCP，通过上面的 Chat Completions / Responses / Messages 接口完成工具调用。[examples/mcp](../examples/mcp) 提供 Exa Streamable HTTP 和 Windows filesystem stdio 的四套配置；真实客户端验收脚本位于 [tests/mcp_smoke.py](../tests/mcp_smoke.py)。

## 图片输入

三个接口共用图片校验、下载和隔离执行逻辑，支持 PNG、JPEG、WebP、静态 GIF，接受 Base64 或公网 HTTP(S) 图片。图片可与客户端工具调用、无工具的 JSON Schema 输出及历史续接配合使用。单张图片最多 10 MiB、1600 万像素，每次请求最多 8 张、合计 20 MiB／3200 万像素。真实 API 和客户端验收脚本分别为 [tests/vision_api_smoke.py](../tests/vision_api_smoke.py) 和 [tests/vision_smoke.py](../tests/vision_smoke.py)。

## 客户端 Skills

Pi、Codex、Claude Code、OpenCode 在客户端发现和加载 `SKILL.md`，代理保留技能指令、引用内容和工具调用；脚本在客户端执行，无需安装到代理服务器。支持自动选择以及客户端各自的显式调用方式。验收脚本 [tests/skills_smoke.py](../tests/skills_smoke.py) 覆盖技能加载、引用读取、脚本执行及独立会话数据检查，使用 `uv run python tests/skills_smoke.py` 运行。

## Agent 会话与进程对应

适用于任意 coding agent / agent 客户端，包括 dsh、pi、Codex、Claude Code、OpenCode。**服务端维护会话与独立 AGY PID / conversation_id 的对应关系，客户端无需新增自定义请求头。** 默认在同一 API 凭据内，按完整历史与上一轮实际回复（包括工具调用 ID）识别连续请求。

客户端若已有会话字段，优先使用它；兼容 `X-Agy-Session-Id`、`X-Session-Id`、`session-id`、`session_id`、`X-Session-Affinity`、`X-Opencode-Session`，以及 `metadata.session_id`、Codex 的 `client_metadata.session_id/thread_id`、Claude Code 的 JSON `metadata.user_id.session_id`。同时出现多个标识时必须一致；普通用户名、请求 ID、API Key 或相同首条 prompt 不会把多个会话合并。

- 客户端仍提交完整历史，代理核对已确认历史后只向同一 stdin 发送新消息。工具回传包含上一条 assistant 的调用 ID，便于关联真实工具结果
- 客户端工具声明和固定桥接规则在 AGY 会话启动时写入私有 agent 配置一次；后续回合不再将其追加到用户消息或对话历史。工具定义、选择或并行约束变化时创建新会话并加载新配置；修复重建也为新进程加载一次原配置
- 单个 PID 同时只执行一轮；显式同会话重叠请求或自动识别出的在途重复续接返回 409，容量全部忙时返回 429
- 没有会话字段也会保留成功进程。唯一匹配到连续历史才复用；首次请求、匹配歧义或并发分支各自建立独立进程
- 历史重试/编辑/截断、模型/effort/工具/schema/附件变化、进程失败或 TTL/LRU 回收时，使用完整历史建立新进程。失败和取消的半轮状态不会交给下一次请求
- 成功且可复用的进程保持 stdin 打开；空闲回收时先关闭 stdin，让 CLI 正常退出。外部工具仍仅在客户端执行，私有工作目录/图片路径校验持续有效

`prompt_cache_key` 本身可能跨会话共享，默认不作为身份；连续历史仍可自动复用。如果客户端原本支持明确的会话标识，可以消除内容完全一致的会话或克隆之间的歧义；无需为 dsh/pi 修改 headers 或缓存配置。协议证据与接入说明见 [Headless 分析](archive/headless-protocol-analysis-20261011.md)，自动会话关联和任务状态排查见 [本次修正记录](archive/session-continuity-and-todos-20261011.md)。

## 已知限制

- **仍经过 agy 的 Agent 层**：客户端工具模式使用自定义主 Agent 限制工具；系统上下文、模型行为与原生模型 API 不完全相同。
- **Gemini 原生接口**支持图片 `inlineData` 与客户端 function calling，能力边界见[统一协议说明](unified-client-protocols.md)。工具/MCP 返回图片暂不支持；`temperature` 等采样参数仍不透传。
- 四种推理协议的普通文本和工具请求都使用受限执行上下文；只允许提交结果，含图片时额外允许读取会话图片白名单。
- 首次请求或会话重建有进程冷启动开销；连续历史可复用已有进程，预热可减少冷启动等待
- 需遵守 Antigravity 服务条款与账号额度，本代理不做额外限流（按 key 只做统计，不做配额；可用 `MAX_SESSIONS` 约束并发）
- 控制台用量只保留**最近 500 条**请求明细（更早的只进总量与按模型聚合），且首次启动前的历史不可追溯
