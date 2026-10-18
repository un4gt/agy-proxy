# AGY Headless 协议分析与通用 agent 会话接入

日期：2026-10-11，北京时间。官方文档已下载至 [HTML 原件](../reference/antigravity-cli-headless.html) 和 [可读文本](../reference/antigravity-cli-headless.md)；[下载来源与 SHA-256](../reference/sources.json)。orjson 的 [README](../reference/orjson-readme.md) 同样已归档。

后续修正：初版只对显式会话 ID 保留 PID，未覆盖默认 dsh 请求。现已补上服务端按完整历史自动关联的路径；不要求客户端新增请求头。最新排查与验证见 [会话连续性和任务状态](session-continuity-and-todos-20261011.md)。本文文末的镜像与 349 项测试是初版部署记录。

## 结论与证据边界

本次修改已部署到服务器，镜像 `agy-proxy:headless-protocol-20261011`。此前失败输入在新实现中重放成功：163.82 秒返回通过校验的客户端工具调用，超过旧 120 秒窗口。真实多轮测试确认同一 PID/conversation 复用，生产 HTTP 首个 SSE 事件约 0.102 秒到达。详细记录见文末与 [运行验证数据](../reference/headless-runtime-verification-20261011.json)。

目标适用于所有 coding agent / agent 客户端，不限 dsh：同一凭据下同一个 agent session，多轮请求复用独立 AGY PID 和 conversation；不同会话隔离。客户端 HTTP 请求、AGY turn、CLI process、AGY conversation 是不同层级。

官方 `--input-format stream-json --output-format stream-json` 正好提供持续 stdin 会话：启动一次，每个 user 输入执行一轮，每轮以 result 结束。不能每轮等进程退出，也不能每个 HTTP 请求都销毁成功会话。

在服务器 CLI 1.3.3 上已实际验证两个 stdin 回合，记录见 [探针结果](../reference/headless-live-probe-20261011.json)：PID 2378，conversation `f5fb2250-cd0e-4e63-9c23-838d8f01697a`，两轮相同；init 一次，result 两次；关闭 stdin 后退出码 0。第一轮墙钟约 7.85 秒，第二轮约 2.72 秒。这证明协议与多轮复用可行，不代表复杂任务都能在该时间内完成。

此前六次 dsh 504 的服务端证据见 [CLI 排查报告](../../test_sessions/agy-cli-investigation-20261011.md)。上游曾产生推理 token，代理因 stdout 静默 120 秒发送 SIGTERM，CLI 随后记录 CLIENT_CANCELED/context canceled。stdout 不是上游完整活动探针。不能据此把所有静默判为卡死，也不能保证任意延长超时后都会成功。

## Streaming JSON 的正确消费方式

| 事件或字段 | 协议含义 | 代理处理 |
| --- | --- | --- |
| init | 每个会话只发一次 | 保存 conversation_id 和运行配置；复用后的第二轮不等待另一个 init |
| init.model / agent | 仅显式设置对应参数时出现 | 不以缺少可选字段判错；限制工具的执行模式必须选中 proxy-external agent |
| permission_mode | 默认 request-review，跳过审批时 always-proceed | 记录实际值；外部工具模式保留原有 agent、hook 和路径限制 |
| step_type=user_input | 输入步骤 | 观察状态，不作为回答输出 |
| step_type=agent_response | 模型响应步骤 | ACTIVE 与 DONE 的 text_delta 都按增量拼接 |
| step_type=tool | CLI 原生工具步骤 | 检查工具及附件路径；不把它误认成客户端 tools/tool_calls |
| step_type=checkpoint | 内部检查点 | 不作为回答或本轮结束标记 |
| state=ACTIVE / DONE | 一步进行中 / 已结束 | DONE 不等于本轮成功；取消后的步骤也可能 DONE |
| result.response | 当前这一轮回答 | 本轮文本的最终依据 |
| result.structured_output | 当前这一轮结构化结果 | --json-schema 只约束终端结果，先校验再发布工具调用 |
| result.usage / num_turns / duration_seconds | 会话累计值 | usage 差分记账；核对 num_turns，避免错认上一轮结果 |

真实测试和官方示例都出现 `ACTIVE: "apple"`，然后 `DONE: "\n"`。旧代码把 DONE 当累计快照，会丢弃最后一段。新解析直接按顺序拼接，`"哈" + "哈\n"` 也必须得到 `"哈哈\n"`，不能用前缀去重吞掉重复字。

真实两轮累计 usage 为：

| 项目 | 第一轮 result | 第二轮 result（累计） | 第二轮应计入 |
| --- | ---: | ---: | ---: |
| input_tokens | 11777 | 23661 | 11884 |
| output_tokens | 27 | 96 | 69 |
| thinking_tokens | 26 | 94 | 68 |
| total_tokens | 11804 | 23757 | 11953 |

output_tokens 已包含 thinking_tokens。再次相加会重复计数。OpenAI 的 reasoning/cache details 和其他协议的对应字段均从本轮差分映射。

## 错误、退出与生命周期

先看本轮 result.status，只把 SUCCESS 当成功。ERROR、CANCELED、INTERRUPTED、INVALID、WAITING、RUNNING 或缺少状态都不能伪装成正常结束。失败 result 保留可读 error；stdout 提前 EOF 时检查实际退出码和 stderr 摘要，即便退出码为 0、没有本轮 result 也算失败。

官方明确：无效 JSON、缺少 event、非 text 内容块会 ERROR 并退出 1；control_request/control_response 或 CLI 自己处理的斜杠命令会 ERROR 并退出 2；未知输入事件可被跳过并写警告。代理只发送合法 user 文本，不向 stdin 塞控制事件、斜杠命令或图像块。图像仍通过已有受限附件机制处理。

stdout 按行读取 bytes，用 orjson 解码；坏 JSON、无效 UTF-8、错误事件形状直接报告协议错误，不再悄悄丢弃后等待超时。未知扩展事件不会变成回答。队列有容量上限，读写具有背压。

成功后保留进程并释放本轮租约。失败、取消、未通过 schema/原生工具校验时销毁该进程。空闲正常回收关闭 stdin，允许退出 0；仍无法退出时才强制终止。单个 PID 不允许交错写入两轮输入，同一会话并发返回 409。

文档写默认 print-timeout 为 5 分钟，服务器版本帮助曾显示 0；不依赖版本默认值，显式按 TURN_TIMEOUT 传入 `--print-timeout`。

## 客户端 stream 与超时

| 客户端请求 | CLI 格式 | HTTP 行为 |
| --- | --- | --- |
| Chat/Responses/Messages 的 stream=false | stdin/stdout stream-json | 逐行消费，等当前 result 后返回 JSON；不等 PID 退出 |
| stream=true | 同上 | 尽快建立 SSE，10 秒一次保活；普通文本增量转发 |
| Gemini generateContent | 同上 | 汇总返回 JSON |
| Gemini streamGenerateContent | 同上 | SSE |
| 工具或结构化输出 + stream=true | 同上 | 等待期间保活；终端结果校验成功后发送完整有效内容/工具参数 |

开始 SSE 后 HTTP 状态已经是 200，后续 CLI 失败通过该协议的错误事件传递，不能再改成 HTTP 502/504，也不会发送成功 finish 事件。参数校验和进程领取失败仍可在 SSE 开始前返回真实 HTTP 错误。客户端断连时关闭迭代器和上游租约。

默认 `AGY_PROXY_IDLE_TIMEOUT=0`，不再因缺少 stdout 而误杀；`AGY_PROXY_TURN_TIMEOUT=600` 仍是总上限，错误信息区分这两种超时。`AGY_PROXY_STREAM_HEARTBEAT=10` 控制 HTTP 保活。Compose 已透传这几个变量，单改 .env 后需重新创建容器才能生效。

心跳不是模型内容，不伪造推理文字或工具进度。dsh 源码的 streamIdleTimeoutMs 默认 300000，并在转换后的模型事件层检查等待时间；SSE 注释不保证能重置这个计时器。若希望客户端等待代理 600 秒总时限，需要把该客户端配置设到更长（例如 660000）。这是客户端等待策略，不能修复上游本身长期不完成的问题。

## 通用会话身份

服务端默认在 API 凭据内比较完整历史、上一轮实际交付的 assistant 回复及工具调用 ID。唯一且连续的匹配复用原 PID 和 conversation，客户端无需发送额外字段。首次请求各自生成内部会话 ID；匹配歧义、配置变化或并发分支重建隔离进程，不按用户名或相同 prompt 合并。

如果客户端原本支持动态会话身份，也可以发送以下可选字段：

```http
Authorization: Bearer <该用户的 API Key>
X-Agy-Session-Id: <本次 agent 会话稳定的唯一 ID>
```

使用显式身份时，每个新建会话、克隆或分支应分配新 ID；不要把固定用户名、设备号、API Key 或请求 ID 当会话 ID。接口支持 Chat Completions、Responses、Messages、Gemini。

还识别以下明确的会话字段：

- X-Session-Id、session-id、session_id、X-Session-Affinity、X-Opencode-Session。
- metadata.session_id。
- Codex 的 client_metadata.session_id / thread_id（turn_id、root_turn_id 不作会话标识）。
- Claude Code 在 metadata.user_id 中编码的 JSON 对象内的 session_id；普通字符串 user_id 不作会话标识。

上述客户端元数据形状已对照本地先前 Codex / Claude Code 请求抓包。多个标识冲突时返回 400。代理只保存凭据分区后的散列和消息指纹，不把 API Key 原文写入会话日志。

收到完整历史后，必须与上轮已确认的输入 + 已交付 assistant 回复连续匹配，才复用常驻 PID。续接 prompt 包含上一条 assistant 的客户端调用 ID 和新工具结果/用户消息，让 AGY 能关联真实客户端工具结果。上下文变化、重试同一请求、历史编辑/压缩、模型/effort/schema/工具/附件变化均从完整历史重建；已失败的隐藏状态不会延续。

空闲 TTL 默认 1800 秒，达到容量时可回收最久未用的空闲进程。代理重启或回收后通过客户端完整历史恢复，当前实现不承诺跨重启保留 PID。所有请求均保持原有外部工具执行限制。

## pi / dsh 的配置

已检查 npm `@earendil-works/pi-ai@1.0.2`：OpenAI Completions 对自定义 baseUrl 默认未必发送 session affinity headers；仅应用层存在 options.sessionId 不代表 HTTP 中已经带了它。

原生 pi 可使用下列模型配置（其中 affinity 字段仅用于可选的显式身份，默认历史关联无需它们）：

```json
{
  "id": "gemini-3.8-flash",
  "name": "Gemini 3.8 Flash",
  "reasoning": true,
  "input": ["text"],
  "contextWindow": 1048576,
  "maxTokens": 65536,
  "compat": {
    "supportsReasoningEffort": true,
    "supportsUsageInStreaming": true,
    "sendSessionAffinityHeaders": true,
    "sessionAffinityFormat": "openai"
  }
}
```

选择 high reasoning 后，API 发送基础模型 ID + `reasoning_effort: "high"`，无需把 -high 写进基础 ID。窗口和输出上限沿用本次用户已配置的值；客户端声明不会自动证明上游实际配额或解除代理输入长度限制。

当前检查到的 dsh 配置 schema 没有开放 sendSessionAffinityHeaders；服务端自动关联已覆盖这种情况。dsh 的 **provider 配置**可保持普通请求，不需要自定义 headers 或改缓存策略：

```json
{
  "api": "openai-completions",
  "baseURL": "http://<服务器>:8317/v1",
  "reasoning": "high",
  "streamIdleTimeoutMs": 660000,
  "models": [{
    "id": "gemini-3.8-flash",
    "name": "Gemini 3.8 Flash",
    "contextWindow": 1048576,
    "maxTokens": 65536,
    "input": ["text"],
    "reasoningEfforts": {"low": "low", "medium": "medium", "high": "high"}
  }]
}
```

保留原有 provider 名称和 apiKeyEnv/凭据配置。不要给整个 provider 写死一个 X-Agy-Session-Id，那会使它的多个聊天声明同一身份。已有 X-Agy-Session-Source: prompt-cache-key 的可选接入方式继续兼容；它不是复用 PID 的前提。

其他 agent 同样默认按历史连续性关联。没有会话字段时，服务端无法区分线上内容完全一致的克隆；若存在多个候选，则建立新进程，而不随机选择。历史重放与分支均不会把不同分支的新消息混进同一个进程。

## orjson 与验证

运行时应用的请求解析、CLI NDJSON、SSE、工具参数、配置和持久化 JSON 都使用 orjson；响应类也覆盖 JSON render，独立工具 hook 直接导入 orjson。热路径直接使用 bytes，不反复编码 UTF-8。标准库 json 仅保留在测试/独立诊断脚本和下载的官方示例中。

orjson.dumps 返回 bytes，canonical JSON 使用 OPT_SORT_KEYS；orjson 不提供 JSONL framing，代理仍负责按行读写。loads 严格拒绝 NaN、Infinity、无效 UTF-8。由于 dumps 默认会把非有限浮点数写成 null，结构化输出校验和 HTTP JSON render 显式拒绝该情况，避免修改语义。

回归测试覆盖：增量 DONE 和重复文本、多轮同 PID、累计用量差分、所有 API 适配器、不同凭据/会话隔离、重试/分叉/配置改变、同会话并发、取消、TTL 正常退出、各终端状态、退出码 0/2/3、坏 NDJSON、静默生成/总时限、工具等待 SSE 保活、无效结构化输出不泄漏，以及多种 agent 身份字段。

最终全量测试：`uv run pytest -q -o faulthandler_timeout=30`，349 passed、2 skipped，耗时 32.04 秒。新增断连回归还覆盖 HTTP 响应头发送失败时释放预领取的进程，以及正常完成后保留可复用会话。10 条警告来自 Starlette/httpx/anyio 的弃用提示。

## 服务器验证与部署结果

1. 隔离镜像测试：同 session 复用 PID、凭据隔离、usage 差分、等待心跳和非法 JSON 拒绝均通过，orjson 3.13.0。
2. 真实 CLI 工具往返：PID 52、conversation `78bac023-f33a-4ac7-bec2-4b708b5f5ecc`。第一轮请求客户端 read，第二轮接受真实传入的工具结果并回答 apple；复用 PID，第二轮 3.54 秒完成，累计 usage 的差分验证通过。
3. 原失败输入重放：从原 conversation 数据库只恢复 user_input，SHA-256 校验为 `c01c81b4f3ffce5a0a82de5249dae8b8718edc18bb0551ad8ccbc0434bd18f65`。使用同一份 9 条客户端消息、27 个工具定义、gemini-3.8-flash + high。约 163.82 秒完成，SSE 心跳 32 次（本次探针间隔 5 秒），无 error，得到有效 todo_write 和 write 调用；write 参数长 64706 字符。这里只验证工具请求的生成与校验，没有执行导出任务里的客户端写文件操作。
4. 重放用量：input 20857、output 26684（其中 thinking 5102）、total 47541。此探针用完整历史新建会话，所以它证明本例可以在取消错误的 stdout 静默判定后完成，不把成功归因于会话复用或 orjson 性能提升，也不保证所有复杂任务永不超时。
5. 生产 HTTP 验证：首个 SSE 事件 0.102 秒，首轮结果 12.128 秒，中间收到一次 10 秒保活；第二轮改用 stream=false，仍 `new_session=0`，两轮总计 15.435 秒。

生产镜像 ID：`sha256:ea86e747d2fcacaa98435b9988b61d0a7de514bd0f91dc155729ed4329b5c913`。CLI 仍为服务器原有 1.3.3。部署核对了 34 个文件哈希，容器健康检查通过；登录、API Key、累计用量、模型禁用设置和原有数据卷均保留。

生效配置：IDLE_TIMEOUT=0、TURN_TIMEOUT=600、STREAM_HEARTBEAT=10、SESSION_TTL=1800。服务器回退备份：`/home/docker_depolyments/agy-proxy/backups/headless-protocol-20261011`；未创建 Git 提交或发布公共镜像。
