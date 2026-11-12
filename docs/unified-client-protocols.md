# 统一客户端协议与工具兼容层

四种 API 均通过固定 SHA 1.4.2 native bridge 推理。协议适配层把 OpenAI Chat、Responses、Anthropic Messages 和 Gemini 的请求规范化为共享的消息与工具契约，再由 `BridgeTurn` 组装 `GetChatMessageRequest`。工具始终由客户端执行；代理不运行服务端 shell、MCP 或文件工具。

## 能力表

| 能力 | Chat Completions | Responses | Anthropic Messages | Gemini |
|---|---|---|---|---|
| 普通文本 | 支持 | 支持 | 支持 | 支持 |
| 客户端函数调用／结果往返 | `tool_calls`／`tool` | `function_call`／`function_call_output` | `tool_use`／`tool_result` | `functionCall`／`functionResponse` |
| 流式与非流式 | 支持 | 支持 | 支持 | 支持 |
| 指定工具、禁用工具、必选工具 | 支持 | 支持 | 支持 | `toolConfig.functionCallingConfig` |
| 并行调用 | `parallel_tool_calls` | `parallel_tool_calls` | `disable_parallel_tool_use` | 多个 `functionCall` |
| namespace／custom | 名称别名映射 | 保留 namespace、custom 文本／grammar 提示 | 名称别名映射 | 名称别名映射 |
| 图片输入 | image_url | input_image | image | inlineData／HTTPS fileData |
| 独立结构化输出 | response_format | text.format | output_config.format | responseMimeType + responseSchema／responseJsonSchema |
| 响应缓存 | 无 | previous_response_id，原有归属、TTL 和大小限制 | 无 | 无，cachedContent 明确拒绝 |

声明工具与独立结构化输出不能叠加。Responses custom grammar 作为生成约束提示传递，不提供本地语法解析器。采样参数、输出长度参数的接受不代表上游提供对应的强制保证。Gemini 内置搜索、代码执行等服务端工具、非阻塞函数、流式函数结果及非文本输出明确返回 400。

Gemini 先按调用 ID 配对结果；未提供 ID 时，只能按名称唯一匹配尚未完成的调用。同名并行调用无 ID 时返回 400，不能猜测配对。

## 契约与执行

简单合法工具名保持不变，命名空间、长名和其他名称映射为确定性哈希别名，声明、指定选择与历史调用一致映射并检查冲突。参数字段从不自动重命名；同名 `bash` 声明 `command+description` 或 `cmd` 时分别遵守自身契约。

保留原始 Schema 和规范化副本。Gemini 大写类型、nullable 转换为等价 JSON Schema；校验遵循声明的已知 Schema 方言，未知方言拒绝。必填项、枚举、组合约束、附加属性和本地引用保持有效；禁止外部引用与校验时的网络访问。Schema 遍历仅进入 Schema 节点，`title`、`description`、`default` 等参数键、常量数据和历史结果不会被清洗。历史调用不按新一轮 Schema 重新校验。

bridge 使用原生 function calling：工具定义以 `ChatToolDefinition` 发送，客户端历史中的 `tool_calls`/`tool` 转成 assistant/tool 消息，Gemini 返回的 `deltaToolCalls` 在参数完整后转回客户端协议。Gemini 的 `thinkingSignature` 按 tool call ID 缓存并持久化，工具续轮时回填；没有签名时上游会拒绝续轮。历史中的未知工具或非法参数在交付前返回 `502`。

无客户端工具时，bridge 只发送文本消息。`response_format` 使用合成的 `structured_output` 工具：代理在完整对象返回后按客户端 Schema 校验，再序列化为客户端要求的 JSON。`json_object` 用字符串包裹任意对象，避免上游把无属性 Schema 当作空参数。

## 流式、错误与用量

bridge 没有 stream-json 的自动恢复或重放。上游失败、参数错误和超时直接返回对应错误，不会用修正提示重跑。客户端断开通过 `(*http.Request).Context()` 取消上游请求，并释放 host 的 in-flight 计数。

流式顺序为开始事件／心跳 → 文本增量 → 最终内容与 `tool_calls` → 成功结束。工具参数在完整校验后才交付，不逐字符流出。结构化输出在完整校验后一次性返回。失败流没有伪造完成事件，也不交付部分工具参数；Chat 失败事件后不再发送 `[DONE]`。

所有 HTTP 响应提供 `X-Agy-Request-Id`，Messages 原有 `request-id` 保留。OpenAI 系列错误包括稳定代码；Messages/Gemini 在原生错误字段内携带原因和关联信息。常见代码：

| 代码 | 含义 |
|---|---|
| `invalid_request` | 请求结构、消息顺序或工具参数非法 |
| `tool_contract_invalid` | 模型请求了未声明的工具 |
| `tool_arguments_invalid` | 实参不符合客户端 Schema |
| `structured_output_missing` | 模型未提交结构化结果 |
| `structured_output_invalid` | 结构化结果不符合 `response_format` |
| `output_json_invalid` | `json_object` 包装内容不是合法 JSON |
| `invalid_tool_schema` | 客户端 Schema 无法解析 |
| `cli_upstream_error` | 上游请求失败 |
| `cli_transport_error` | bridge host 传输失败 |
| `upstream_timeout` | 单轮总预算耗尽 |

用量来自 bridge 的 `inputTokens`、`outputTokens`、`thinkingOutputTokens` 和 `cacheReadTokens`；OpenAI Responses/Gemini 在响应层恢复缓存输入与总量。每个 HTTP 请求只写一条最终记录，缺失用量标记 `usage_complete=false`，不估算。日志只记录关联信息、Schema 指纹、名称、路径、约束和结果，不打印脚本、完整参数或正文。

## 兼容用例与验证

```bash
uv run pytest -q
```

- `core/tests/test_bridge_protocol.py`：模型映射、请求结构、签名、工具分片、usage、图片、结构化输出、签名持久化。
- `core/tests/test_bridge_turn.py`：文本增量、工具重组、签名落盘、结构化结果校验。
- `tests/test_bridge_api.py`：Chat Completions 经 bridge-only 应用路由，健康接口报告 `chat_backend=bridge`。

真实 bridge 冒烟需要 Linux 和已登录的固定 SHA 1.4.2 二进制；2026-11-11 在本地容器验证了文本、流式、原生工具调用、工具续轮、图片、取消和接近 1M 输入。旧 stream-json/GDB 实现及对应测试位于 `legay_stream_json_version` 分支。

## 迁移说明

main 不再接受 `AGY_PROXY_BRIDGE=false`、`AGY_PROXY_NATIVE_CHAT`、`AGY_PROXY_AGENT`、`AGY_PROXY_SKIP_PERMISSIONS` 或 `AGY_PROXY_SANDBOX`。升级时删除这些环境变量，保留 `AGY_PROXY_BIN=/opt/agy-bridge/agy`。固定 SHA 副本不能通过控制台更新，升级必须重建镜像。
