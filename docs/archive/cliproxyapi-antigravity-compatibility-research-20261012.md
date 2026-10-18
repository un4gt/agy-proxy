# CLIProxyAPI 的 Antigravity 与多 Agent 兼容机制调研

日期：2026-10-12。先通过 DeepWiki 定位架构，再逐项核对 GitHub 源码。固定研究版本为 `696bbaee031ca39e3b15cd65b187d90ef0d3ef5d`（main 快照，非已验证的生产版本）。本次未部署 CLIProxyAPI、导入账号或发送真实模型请求。

## 核心判断

同为 Chat Completions 不代表工具契约相同：工具名、必填字段、Schema 方言、历史消息组织及 SSE 消费行为均可能不同。维护机制应围绕请求声明的契约和上游能力组织，而不是不断增加“某 Agent 的 bash 应该使用哪个参数名”的特例。

CLIProxyAPI 最值得借鉴的是原生工具调用与分层转换。它的 Antigravity 路径使用 OAuth 凭据直接请求 Cloud Code HTTP 接口，不通过本地 agy CLI 的 stdin/stdout 驱动一个代理 Agent。两者虽然都使用 Antigravity 账号，却不是同一执行架构。

同时，CLIProxyAPI 并没有在已核对的 Antigravity → Chat Completions 普通返回路径上实现“按原始客户端 Schema 全量校验，缺字段后让模型自动修正”的通用闭环。因此，它的实现不能证明换用它就一定不会出现 cmd/command 错配。

## 登录与推理路径

- [AntigravityAuthenticator.Login](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/sdk/auth/antigravity.go#L34)：浏览器 OAuth/手动回调、交换 access/refresh token、获取账号身份与 project ID。
- [AntigravityExecutor.Execute](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/runtime/executor/antigravity_executor_execute.go#L26)：按 SourceFormat 选择转换，获取 access token，再进入上游请求。
- [buildRequest](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/runtime/executor/antigravity_executor_request.go#L27)：构造 HTTP POST，设置 Bearer token，调用 generate/stream 路径。
- [端点常量](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/runtime/executor/antigravity_executor.go#L38)：`/v1internal:generateContent` 与 `/v1internal:streamGenerateContent`。

因此，“支持 Antigravity 登录”准确；“必须启动并控制 Antigravity CLI 来转发”不适用于这里核对的路径。Gemini CLI 与 Antigravity 在项目中也是不同 provider，不能因名称接近而混用结论。

## 转换链与我们当前的区别

| 环节 | CLIProxyAPI 的 Antigravity 路径 | 当前 agy-proxy 外部工具路径 |
|---|---|---|
| 客户端 tools | 转为上游原生 functionDeclarations | 工具定义写进 Agent 提示词 |
| 生成时的工具契约 | 上游收到各工具转换后的参数 Schema | finish 主要约束外层；arguments 只要求 object |
| 模型产物 | 原生 functionCall.name/args | finish 中描述要交给客户端的 tool_calls |
| 返回客户端 | 序列化 args，恢复工具名及客户端协议 | 按客户端原始 Schema 校验后交付 |
| 不合法参数 | 已核对返回转换主要透传序列化，无通用参数修正循环 | 直接 AgyError，未提供修正机会 |
| 会话基础 | HTTP 请求及历史/签名/状态转换 | 常驻 CLI 进程、内部 conversation 与客户端历史关联 |

这不意味着原生 function calling 能保证每次输出有效；上游能力和 Schema 降级仍会影响约束强度。它减少的是我们当前额外引入的“让一个 Agent 模拟另一个 Agent 工具协议”的环节。

## 如何控制兼容性维护范围

### 1. 按协议组合注册转换器

[registry](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/sdk/translator/registry.go#L29) 以 from/to Format 注册请求和返回转换。[OpenAI → Antigravity 注册](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/translator/antigravity/openai/chat-completions/init.go#L9) 分别指定 request、stream response、non-stream response。

主要转换依据是协议和请求体。项目也存在特定客户端兼容路径，例如 [Anthropic models 的请求头识别](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/api/server_routes.go#L585)、Claude Code 身份/请求适配以及独立 Amp 模块；因此不能概括成“完全没有客户端特例”。更合理的借鉴是让特例留在明确的适配边界内。

### 2. 工具定义进入原生声明，保留名称映射

[请求转换](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/translator/antigravity/openai/chat-completions/antigravity_openai_request.go#L337) 将 tools[].function 转成 functionDeclarations，处理参数 Schema、工具名合法化和去重。名称映射使用碰撞消解，返回时恢复客户端名称。

[tool_choice 转换](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/translator/antigravity/openai/chat-completions/antigravity_openai_request.go#L501) 映射 none/auto/required/指定函数；none 除设置 NONE 外，还删除工具声明。

转换不会把所有名为 bash 的工具强行改成同一入参形状。客户端声明 command 就应尊重 command；声明 cmd 就应尊重 cmd。这是契约数据，不应该是按 Agent 名称维护的业务规则。

### 3. Schema 清洗集中处理，严格限定作用范围

[CleanJSONSchemaForAntigravityTool](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/util/gemini_schema.go#L51) 和 [cleanJSONSchema](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/util/gemini_schema.go#L113) 集中处理本地引用、复杂组合、缺失 items、元数据与后端不支持的约束。部分约束会降级为 description 提示，部分结构会投影/简化，所以不应称其为完全无损转换。

特别值得借鉴的是 [sanitizeAntigravityRequestSchemas](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/runtime/executor/antigravity_executor_request.go#L160)：只清洗承载 Schema 的子树，不对整份请求递归删字段。源码注释明确记录，过去对整个请求清洗曾误删历史函数实参的 title/format/default 等普通业务字段，模型随后模仿被破坏的例子。

[对应回归测试](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/runtime/executor/antigravity_schema_sanitize_test.go#L44) 比较清洗前后的 request.contents，要求历史内容保持原样。

### 4. 返回事件的兼容性单独维护

[流式响应转换](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/translator/antigravity/openai/chat-completions/antigravity_openai_response.go#L175) 将 functionCall.args.Raw 作为 OpenAI function.arguments 字符串交付，生成调用 ID 和 index，并维护 finish_reason、usage 等状态。

[非流式路径](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/translator/antigravity/openai/chat-completions/antigravity_openai_response.go#L321) 先恢复工具名，再委托 Gemini → OpenAI 转换；[委托路径](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/internal/translator/gemini/openai/chat-completions/gemini_openai_response.go#L395) 同样序列化 args。

这里未见按原始工具参数 Schema 校验并自动修正生成参数的步骤。恢复工具名称也不等于猜测参数字段重命名。DeepWiki 提到的非法 JSON 包进 params，是请求转换中处理历史 assistant 调用的分支，不能当成新生成参数的自动修复机制。

### 5. 重试按错误和交付阶段控制

[bootstrap retry](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/sdk/api/handlers/handlers_stream.go#L484) 在得到首个可交付 payload 前处理流错误；可重试范围包括未知状态、部分鉴权/限流/超时和 5xx，次数由配置限制。默认 bootstrap retries 为 0，Home 模式另行禁用本层重试。

一旦进入下游数据交付阶段，后续错误终止流，没有回到该 bootstrap 重试循环。[测试](https://github.com/router-for-me/CLIProxyAPI/blob/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d/sdk/api/handlers/handlers_stream_bootstrap_test.go#L809) 明确覆盖已发 partial 后只执行一次。

这些是流启动/上游失败恢复，不是“收到非法 bash 参数后自动修复”的证据。对我们的服务，应分别定义 HTTP/SSE 状态已提交、业务内容已交付、工具调用已交付三个边界，不能简单以 HTTP 200 或心跳判断是否能安全重新生成。

## GitHub 中实际出现的兼容案例

这些问题在检索时均为 closed，但关闭状态本身不代表所有版本都已修复：

| 问题 | 说明 |
|---|---|
| [#5193](https://github.com/router-for-me/CLIProxyAPI/issues/5193) Pi/TypeBox 的 ~optional | 同样是 /v1/chat/completions，工具 Schema 带内部元数据后被 Gemini 拒绝；需要在 Schema 层处理 |
| [#6539](https://github.com/router-for-me/CLIProxyAPI/issues/6539) dependentRequired | 附加某个 MCP 工具才失败，表现像偶发；实质是上游不接受的 Schema 关键字 |
| [#5292](https://github.com/router-for-me/CLIProxyAPI/issues/5292) array 缺 items | 下游接受的工具定义不一定被 Gemini 接受 |
| [#5485](https://github.com/router-for-me/CLIProxyAPI/issues/5485) tool_choice=none | 单独设置上游 mode 不足以保证行为，需要转换层落实禁止工具的结果约束 |

这些案例支持用户的维护担忧，也说明成熟项目仍需持续维护。合理目标是一个缺陷修复覆盖一类请求，而不是承诺协议相同就行为完全相同。

## 对 agy-proxy 的建议

### 近期：建立通用工具契约层

建议保存每次请求的 ToolContract：原始工具名、原始参数 Schema、上游适配后的 Schema、必要的可逆名称映射及能力限制。原始契约用于交付校验，适配结果用于生成约束，两者不能混为一份并相互覆盖。

- 将 JSON 语法错误、桥接协议错误、工具实参校验错误、网络/额度错误分开分类与记录。
- 对工具实参校验失败，在未交付业务内容/工具调用且确认无副作用的前提下，最多一次反馈精确错误并要求重新生成，再完整校验。
- 不全局进行 cmd → command 等语义猜测，也不填造必填 description；这些名称可能在另一个客户端中有其他含义。
- 诊断只记录工具名、Schema 指纹、错误路径、validator、缺失字段和关联标识，不记录用户脚本正文。
- 保留已有会话隔离与失败状态清理机制，修正重试不能复用未经处理的失败隐藏状态。
- 评估 CLI 支持的 Schema 子集后，再决定是否把按工具区分的约束放进 finish；不能未经验证照搬 oneOf/$ref。

### 验证矩阵按能力组织

至少包括：command+description 与 cmd 两类合法客户端契约；同名工具不同 Schema；本地 $ref/anyOf/enum/空参数/附加属性；名称合法化碰撞；多工具调用和结果配对；历史实参包含 title/default/description；长命令与引号换行；tool_choice 各种模式；流/非流；流开始前错误、心跳后错误、工具调用交付后错误；同一会话工具配置变化。

用脱敏请求形状建立固定 fixtures，同一行为跨 Agent 复用。客户端 SDK 差异（特别是流内错误分类）再单独做少量集成测试。这样可以定位“哪个能力不兼容”，减少“某客户端偶尔不行”的模糊问题单。

### 长期：单独评估原生工具调用后端

若主要目标是为多个 Agent 提供稳定的模型 API，建议评估复用 CLIProxyAPI 作为后端或增加原生 HTTP executor，让工具定义走原生 function calling。接入前用同一套契约 fixtures 比较，确认模型、额度、鉴权、签名/历史与流行为。

这属于后端架构选择，不能把现有 CLI 凭据路径和生命周期直接照搬过去。直接 HTTP 也带来 OAuth、私有上游接口、Schema 子集及签名兼容维护；维护成本并未消失。优先复用成熟后端通常比另写一整套 HTTP 适配更省事，但仍需实际兼容验证。

若必须继续依赖官方 agy CLI，应明确接受 finish 桥接边界，并先完成通用校验/受限恢复机制。仅持续补提示词或为 DSH 单独改字段，无法形成长期稳定的兼容层。

## 调研来源与验证边界

- [DeepWiki 架构问答](https://deepwiki.com/search/explain-antigravity-integratio_65e8fb39-675f-407a-b90f-c30af462d7cb)
- [DeepWiki 兼容与重试问答](https://deepwiki.com/search/focus-on-compatibility-archite_79e37c78-7b14-420b-8e05-c114690fbf83)
- [固定源码版本](https://github.com/router-for-me/CLIProxyAPI/tree/696bbaee031ca39e3b15cd65b187d90ef0d3ef5d)
- 上文 GitHub Issues 为问题报告；具体行为以固定版本源码为准。
- 本次进行了静态源码交叉核对及相关测试代码阅读，没有运行 CLIProxyAPI 测试套件或开展在线对比；不作吞吐、成功率或故障率优于当前服务的结论。
