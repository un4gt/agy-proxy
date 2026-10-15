# 统一客户端协议与工具兼容层

四种 API 均通过 AGY CLI 推理。代理将协议解析与执行分开，使用 `CanonicalRequest`、`ToolContract`、`ToolCall` 和 `ExecutionOutcome`，由静态适配器注册表连接协议边界与共享执行器。

## 能力表

| 能力 | Chat Completions | Responses | Anthropic Messages | Gemini |
|---|---|---|---|---|
| 普通文本、受限执行 | 支持 | 支持 | 支持 | 支持 |
| 客户端函数调用／结果往返 | `tool_calls`／`tool` | `function_call`／`function_call_output` | `tool_use`／`tool_result` | `functionCall`／`functionResponse` |
| 流式与非流式 | 支持 | 支持 | 支持 | 支持 |
| 指定工具、禁用工具、必选工具 | 支持 | 支持 | 支持 | `toolConfig.functionCallingConfig` |
| 并行调用 | `parallel_tool_calls` | `parallel_tool_calls` | `disable_parallel_tool_use` | 多个 `functionCall` |
| namespace／custom | 名称别名映射 | 保留 namespace、custom 文本／grammar 提示 | 名称别名映射 | 名称别名映射 |
| 图片输入 | image_url | input_image | image | inlineData／HTTPS fileData |
| 独立结构化输出 | response_format | text.format | output_config.format | responseMimeType + responseSchema／responseJsonSchema |
| 响应缓存 | 无 | previous_response_id，原有归属、TTL 和大小限制 | 无 | 无，cachedContent 明确拒绝 |

声明工具与独立结构化输出不能叠加。Responses custom grammar 作为生成约束提示传递，原有兼容层不提供本地语法解析器。采样参数、输出长度参数的接受不代表 CLI 提供对应的强制保证。Gemini 内置搜索、代码执行等服务端工具、非阻塞函数、流式函数结果及非文本输出明确返回 400。

Gemini 先按调用 ID 配对结果；未提供 ID 时，只能按名称唯一匹配尚未完成的调用。同名并行调用无 ID 时返回 400，不能猜测配对。

## 契约与执行

简单合法工具名保持不变，命名空间、长名和其他名称映射为确定性哈希别名，声明、指定选择与历史调用一致映射并检查冲突。参数字段从不自动重命名；同名 `bash` 声明 `command+description` 或 `cmd` 时分别遵守自身契约。

保留原始 Schema 和规范化副本。Gemini 大写类型、nullable 转换为等价 JSON Schema；校验遵循声明的已知 Schema 方言，未知方言拒绝。必填项、枚举、组合约束、附加属性和本地引用保持有效；禁止外部引用与校验时的网络访问。Schema 遍历仅进入 Schema 节点，`title`、`description`、`default` 等参数键、常量数据和历史结果不会被清洗。历史调用不按新一轮 Schema 重新校验。

CLI 仍使用已验证的 finish 外层结构，完整工具 Schema 与固定桥接规则在会话启动时写入私有 `.agents/agents/proxy-external.md`，不随每轮用户消息重复追加。客户端继续按原协议提交工具定义；代理以配置和已确认历史判断能否复用会话。配置变化时创建新进程并加载一次新配置，参数修复的新进程也加载一次原配置；配置不会混入重放的客户端历史。

所有候选在交付前完整校验。只允许 finish；含图片时额外允许读取当前会话的图片白名单。无工具文本请求也无法调用服务端 shell、MCP、文件系统或代理工具。

## 恢复、流式与诊断

`AGY_PROXY_RECOVERY_ATTEMPTS=1` 默认允许每个 HTTP 请求最多恢复一次，设为 0 关闭。参数修正、明确可恢复的瞬时传输错误、配额换号共用预算，不叠加循环。CLI 退出或未知上游故障不凭猜测重试。输入错误、鉴权、取消、超时、协议损坏和原生工具违规不恢复。

参数错误返回工具名、字段路径、约束、缺失字段和校验器信息。修正使用保留模型、effort、账号的新进程，重放完整客户端历史及本次候选和反馈。请求租约跨重建保持；恢复进程完成后销毁，失败候选和修正提示不会写入客户端历史或 Responses 缓存。两次尝试共用截止时间，断开会取消执行并释放进程。

流式顺序为开始事件／心跳 → 校验通过的内容和调用 → 成功结束。开始事件、心跳不消耗恢复资格；任何正文交付后不重新生成。失败流没有伪造完成事件，也不交付部分工具参数。Chat 失败事件后不再发送成功终止标记 `[DONE]`。

所有 HTTP 响应提供 `X-Agy-Request-Id`，Messages 原有 `request-id` 保留。OpenAI 系列错误包括稳定代码；Messages/Gemini 在原生错误字段内携带原因和关联信息。常见代码：

| 代码 | 含义 |
|---|---|
| `output_json_invalid` | 候选不是合法 JSON |
| `bridge_structure_invalid` | finish 桥接结构、工具选择或调用数量错误 |
| `tool_arguments_invalid` | 实参不符合客户端 Schema |
| `cli_transport_error` | CLI 传输失败；只对明确可恢复的情况恢复 |
| `cli_protocol_error` | CLI 事件结构或顺序损坏 |
| `native_tool_violation` | 试图执行受限模式之外的原生工具 |
| `upstream_timeout` | 请求总预算或配置的静默超时耗尽 |
| `quota_exhausted` | 明确的账号配额耗尽 |

用量在校验前收集，各次尝试已知消耗累计；每个请求只写一条最终记录。记录增加 `attempts`、`repair_attempts`、`error_code`、`usage_complete`、`protocol` 和 `request_id`，旧记录可无这些字段。缺失用量不会估算，`usage_complete=false`。日志只记录关联信息、Schema 指纹、名称、路径、约束、尝试次数和恢复结果，不打印脚本、完整参数或 ValidationError。

## 兼容用例与验证

运行回归测试：

```powershell
.venv/Scripts/python.exe -m pytest -q
```

`tests/test_unified_execution.py` 覆盖四协议 × 流式／非流式 × 普通文本／单工具／指定工具／并行工具，以及不同 bash 契约、长名称、Schema 方言、本地引用、组合约束、无 ID 歧义、修正失败、共用恢复预算、总超时、断开与重建期间并发隔离。会话配置回归还检查同 PID 连续回合不追加工具声明、配置变化后新进程仅注册一次，以及修复重建不把配置和失败反馈混入已确认历史。

真实验收使用现有 AGY 登录和随机本地端口，测试项目与客户端配置位于忽略目录 `.preview/unified-cli-*`：

```powershell
.venv/Scripts/python.exe tests/unified_cli_smoke.py --clients pi codex claude opencode
.venv/Scripts/python.exe tests/unified_cli_smoke.py --failure-only
```

脚本验证四协议函数往返、图片，以及真实客户端通过 MCP 修改隔离项目、执行测试并续用第二轮对话。不会修改用户全局客户端设置或生产实例。DSH 未安装时需在具备该客户端的环境补验；与 Pi 共用 Chat 契约的模拟测试不能替代 DSH 实机验收。

2026-10-12 验收：AGY 1.3.3 / gemini-3.8-flash-low，四协议 8 组流式／非流式函数往返、4 组图片、4 组流内失败均通过；Pi 1.1.0、Codex 0.161.0、Claude Code 2.1.295、OpenCode 1.4.6 各两轮 MCP／shell 测试通过。模拟回归 482 通过、2 项原有跳过。详见[脱敏验收报告](reference/unified-client-acceptance-20261012.json)。本机无 DSH 可执行文件，未宣称 DSH 实机通过。

## 发布说明

这是行为变更：普通 Chat/Gemini 不再使用允许服务端工具的旧执行路径，文本流改为整轮校验后交付；相关全局 agent／跳过权限参数不作用于四种推理 API。首次正文可能更晚，等待期间持续心跳。无效工具调用现在最多自动修正一次，最终失败可按错误代码和关联 ID 定位。

升级时同步 `.env.example`、Compose 的 `AGY_PROXY_RECOVERY_ATTEMPTS` 和配置文档。本变更不自动提交、部署或切换生产流量。

2026-10-13：移除所有模型、四种推理协议及 Messages token 估算接口的 200,000 字符检查。长消息、工具定义和工具结果完整传给 AGY，代理不代替 AGY 判断模型上下文窗口，也不截断历史。HTTP 请求体和图片资源限制独立保留。

2026-10-13：工具声明和固定桥接规则改为会话启动时加载一次，避免在复用进程时仍逐轮追加到用户历史。配置变化与恢复重建分别在新进程中加载一次。详见[修复及验收记录](session-tool-configuration-fix-20261013.md)。
