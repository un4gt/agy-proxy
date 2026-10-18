# agy CLI 二进制逆向：调用链、请求参数与指纹

日期：2026-10-16。样本：`bin/agy.exe`，SHA 未单独记录（193,113,240 字节）。
方法：静态字符串/符号/描述符提取（`strings` + Go `rawDesc` 解析），未执行样本、未抓真实流量；网络链路部分与本仓库已有的实测证据交叉核对，来源标注为[实测]或[反推]。

结论的边界：本文是二进制静态分析的产物。凡是标注[实测]的行有服务器端证据；标注[反推]的行来自二进制内嵌的描述符、模板与符号表，能确定"代码里有这条路径/这个字段"，但不等于"该版本一定走这条分支"。

---

## 1. 二进制概况

| 项目 | 值 |
|---|---|
| 格式 | PE32+ x86-64，console 子系统，14 节 |
| 大小 | 193,113,240 字节（`.text` 55.8 MB，`.rdata` 105.6 MB） |
| 语言/工具链 | Go，`go1.28-20260921-RC02 cl/985595000 +644cd61a4a X:boringcrypto,simd` |
| 节名特征 | `flags_he` / `google_i` / `malloc_h` / `protodes` — Go 链接器节名截断残留 |
| 符号 | Go pclntab 基本完整，含大量 `google3/...` 内部路径 |
| 身份 | Antigravity CLI（`agy`），内嵌 `Google Antigravity` / `antigravity-client` 标识 |

符号表里可读到 Google 内部源码路径，说明这是内部构建外发版；据此可以还原出模块结构，而不是靠猜。

**总规模**：`google3/third_party/jetski/` 下的包路径数百个，其中 `cli/*` 45 个子包、`language_server/*` 覆盖代理层、`cortex/*` 覆盖 agent 核心。

---

## 2. 进程内分层

```
cli/entrypoints          入口（TUI / headless / print）
  cli/model, cli/render  BubbleTea 风格 TUI
  cli/backend/backend    ServerBackend：本地状态 + 远程 LS 客户端
  cli/printmode          -p / --print 非交互模式
  cli/headless           --input-format / --output-format stream-json
language_server          language server 主体（Turn 编排、会话、工具执行）
  language_server/code_assist_client   ← 真正发 HTTP 的一层
    auth_provider / auth_provider_external / gemini_api_key_auth_provider
    http_helpers / stream_helpers / model_configs / quota_manager
  language_server/cortex   agent 循环（CortexStep 状态机）
    cortex/core/tools/tools  内置工具实现
    cortex/prompt/template_provider  系统提示词模板
  language_server/installer, updater  自更新
third_party/jetski/*     共享库（protobuf、telemetry、telemetry/cli）
```

关键点：`ServerBackend`（`cli/backend`）不做 HTTP；**所有对上游的调用都收敛在 `language_server/code_assist_client`**。逆向时只需盯住这一层。

---

## 3. 网络链路

### 3.1 上游主机

| 主机 | 用途 |
|---|---|
| `https://daily-cloudcode-pa.googleapis.com` | 生产主端点[实测] |
| `https://preprod-daily-cloudcode-pa.sandbox.googleapis.com` | 预发沙箱[反推] |
| `https://autopush-cloudcode-pa.sandbox.googleapis.com` | autopush[反推] |
| `https://aicode.googleapis.com` | Business AI Code[反推] |
| `https://aiplatform.googleapis.com`、`https://{location}-aiplatform.googleapis.com` | Vertex 直连[反推] |
| `https://generativelanguage.googleapis.com` | Gemini API Key 模式[反推] |

`CloudCodeServerURL` 可被环境变量 **`CLOUD_CODE_URL`** 覆盖[反推]（日志串 `Overriding CloudCodeServerURL via CLOUD_CODE_URL environment variable: %q`）。这对代理很重要：可以不改二进制直接指向自建端点。

### 3.2 RPC 面

服务定义在 `google/internal/cloud/code/v1internal/prediction_service.proto` 等描述符里，走 Google API 的 REST 转码风格（`POST /v1internal:<method>`）。

**实测出现过的调用**（来自 `docs/reference/dsh-taskbox-followup-evidence-20261014.json` 的 212 条网络事件）：

| 次数 | 路径 |
|---:|---|
| 105 | `/v1internal:listExperiments` |
| 29 | `/v1internal:loadCodeAssist` |
| 27 | `/v1internal:recordTrajectoryAnalytics` |
| 18 | `/v1internal:fetchAvailableModels` |
| 11 | `/v1internal:fetchUserInfo` |
| 6 | `/v1internal:retrieveUserQuotaSummary` |
| 7 | `/v1internal:streamGenerateContent` |
| 4 | `/v1internal:fetchAdminControls` |
| 4 | `/v1internal:writeTrajectoryAcls` |

启动阶段的固定序列[实测]：

```
loadCodeAssist → fetchUserInfo → fetchAdminControls → fetchAvailableModels
→ listExperiments（持续轮询，105/212 ≈ 半数流量）
→ streamGenerateContent（每轮对话一次）
```

`listExperiments` 高分频出现，说明实验开关轮询是常态流量，不是异常。

二进制里共 65 个 `v1internal:<method>`，另含 `countTokens`、`onboardUser`、`generateChat`、`completeCode`、`generateImage`、`checkUrlDenylist`、`checkUrlAntivirus`、`registerInteraction`、`sendSignalingMessage` 等。

### 3.3 生成请求的精确形状

这是最关键的一处。`prediction_service.proto` 里 `GenerateContent` / `StreamGenerateContent` 的请求体是：

```proto
// google.internal.cloud.code.v1internal.GenerateContentRequest
message GenerateContentRequest {
  string project = ?;                       // (google.api.resource_reference) RESOURCE
  string request_id = ?;
  google.cloud.aiplatform.master.GenerateContentRequest request = ?;  // 真正的模型请求
  string model = ?;
  string user_prompt_id = ?;
  string user_agent = ?;
  string request_type = ?;
  repeated Credits.CreditType enabled_credit_types = ?;
}
```

外层是 Cloud Code 的壳（项目、路由、计费），内层 `request` 才是标准 `aiplatform.GenerateContentRequest`（`contents` / `system_instruction` / `tools` / `tool_config` / `generation_config` / `safety_settings`）。

调用形式：

```
POST https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse
```

[反推] 里的等价串：
- `{model}:streamGenerateContent?alt=sse`（Vertex 形态）
- `%s/v1beta/models/%s:streamGenerateContent?alt=sse`（generativelanguage 形态）

`request_type` 有 14 个取值，用来给上游标注这次调用是什么：

```
REQUEST_TYPE_UNSPECIFIED / GENERAL / CASCADE / COMMAND / CHECKPOINT /
PLAN / FAST_APPLY / COMMIT_MESSAGE / MQUERY / CONTEXT_CHECK /
CUSTOM_AGENT / ANTIGRAVITY_REVIEW / AGENT_MONITORING / EVAL
```

对话主路径用 `CASCADE`；`AGENT_MONITORING` 与 `ANTIGRAVITY_REVIEW` 对应第 7 节的安全监控；`COMMIT_MESSAGE` 对应生成 commit message 的小提示词。

`retrieveUserQuotaSummary` 的响应结构也在描述符里：`buckets[].{remaining_amount, remaining_fraction, reset_time, token_type, model_id}`——这就是代理做"额度耗尽判定"的上游依据。

---

## 4. 指纹

### 4.1 请求头里带的身份：`ClientMetadata`

```proto
message ClientMetadata {
  IdeType    ide_type;        // enum
  string     ide_version;
  string     plugin_version;
  Platform   platform;        // enum
  string     update_channel;
  string     duet_project;
  PluginType plugin_type;     // enum
  string     ide_name;
}
```

枚举取值（从描述符逐字读出）：

- `IdeType`：`IDE_UNSPECIFIED` `VSCODE` `INTELLIJ` `VSCODE_CLOUD_WORKSTATION` `INTELLIJ_CLOUD_WORKSTATION` `CLOUD_SHELL` `CIDER` `CLOUD_RUN` `ANDROID_STUDIO` **`ANTIGRAVITY`** `JETSKI` `COLAB` `FIREBASE` `CHROME_DEVTOOLS` **`GEMINI_CLI`**
- `Platform`：`PLATFORM_UNSPECIFIED` `DARWIN_AMD64` `DARWIN_ARM64` `LINUX_AMD64` `LINUX_ARM64` **`WINDOWS_AMD64`**
- `PluginType`：`PLUGIN_UNSPECIFIED` `CLOUD_CODE` `GEMINI` `AIPLUGIN_INTELLIJ` `AIPLUGIN_STUDIO` `PANTHEON`

CLI 场景下 `ide_type=ANTIGRAVITY`、`platform` 取当前 OS/arch、`plugin_type=CLOUD_CODE`。`ide_version`/`plugin_version` 就是 CLI 版本。**这三个字段合起来足以把"同一台机器的同一个 CLI 版本"认出来**，但本身不含随机量。

### 4.2 请求体里带的身份

生成请求体直接有 `user_agent` 字段，所以 UA 不只出现在 HTTP 头里，也进请求体。

### 4.3 持久安装标识

| 项 | 位置 | 说明 |
|---|---|---|
| `installation_id` | `~/.gemini/antigravity-cli/installation_id` | 持久 UUID，符号 `installationid.Get` / `AntigravityIdeInstallationID` |
| OAuth 凭据 | `~/.gemini/antigravity-cli/jetski-standalone-oauth-token` | 该 CLI 实际读的凭据文件 |
| CLI 状态 | `jetski_state.pbtxt` | 与凭据放在一起 |
| IDE 安装标识 | `antigravity_ide_installation_id` | IDE 侧，另一个键 |

`installation_id` 与本仓库 `docs/account-isolation.md` 描述一致：这是 CLI 认定的"这台机器这次安装"的身份，多账号隔离时必须每个账号各持一份固定值。二进制的 `installationid` 包与 `language_server.setUpInstallationID` 符号确认了这一路径。

另有 Google 设备侧标识 `device_id` / `device_id_zwieback` / zwieback cookie 一族（`GetZwiebackToken`、`GetGmsDeviceIdZwieback` 等符号），属于平台侧关联标识，CLI 不改写。

### 4.4 OAuth

| 项 | 值 |
|---|---|
| client_id (1) | `1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com` |
| client_id (2) | `884354919052-36trc1jjb3tguiac32ov6cod268c5blh.apps.googleusercontent.com` |
| scopes | `cloud-platform`、`aicode`、`cclog`、`experimentsandconfigs`、`userinfo.profile`、`drive.*`（appdata/apps.readonly/file/metadata/metadata.readonly/photos.readonly） |

认证方式（`auth_provider` 一族符号）：OAuth loopback、ADC（`AGY_ADC_AUTH`）、Gemini API Key、WIF（Workload Identity Federation）、Gateway。

**这是最像"账号指纹"的部分**：`installation_id` + OAuth refresh token 一起决定上游看到的账号身份。代理要换号就必须同时换这两样，只换 token 会被 `installation_id` 串起来。

---

## 5. 系统提示词

模板都在 `cortex/prompt/template_provider/templates/`，从符号表能读出文件名清单：

```
system_prompts/identity.tmpl                 主身份
system_prompts/communication_style.tmpl     沟通风格
system_prompts/artifacts.tmpl               artifact 规范
system_prompts/persistent_context.tmpl      KI（知识项）
system_prompts/skills.tmpl                  技能
system_prompts/plugins.tmpl                 插件
system_prompts/slash_commands.tmpl          斜杠命令推荐
system_prompts/ephemeral_message.tmpl       系统注入消息
system_prompts/conversation_transcript.tmpl 会话记录
system_prompts/function_call.tmpl           函数调用
system_prompts/guidelines.tmpl              行为准则
system_prompts/test_funcmap.tmpl            模板函数
system_prompts/helpers/async_termination_rule.tmpl  后台任务收尾规则
system_prompts/helpers/errors.tmpl          templateError
step_strings/run_command.tmpl               run_command 步骤渲染
step_strings/send_command_input.tmpl        交互式命令渲染
step_strings/helpers/default_status_output_string.tmpl
```

### 5.1 身份模板（原样，含 Go 模板语法）

```
You are Antigravity, a powerful agentic AI coding assistant designed by the Google Deepmind team working on Advanced Agentic Coding.
You are {{if .IsAutonomous}}working for{{else}}pair programming with{{end}} a USER to solve their coding task. The task may require creating a new codebase, modifying or debugging an existing codebase, or simply answering a question.
The USER will send you requests, which you must always prioritize addressing. User requests are enclosed within <USER_REQUEST> tags.
{{- if .IsAutonomous}}
The USER is not available to respond to questions or requests for approval. Work autonomously.
{{- end}}
```

另有一句变体 `You are Antigravity Agent, a powerful agentic AI coding assistant designed by the Google engineering team.` 用于不同入口。

### 5.2 工具调用提示词（两种形态）

**形态 A — 包装式**（模型只看到 `<tools>` 文本清单，靠 XML 解析调用）：

```
You are a tool calling agent. You are provided with function signatures within <tools> </tools> XML tags. You may call one or more functions to assist with the user query. If available tools are not relevant in assisting with user query, just respond in natural conversational language. Don't make assumptions about what values to plug into functions. After calling & executing the functions, you will be provided with function results within <tool_response> </tool_response> XML tags.
<tools>
%s
</tools>
For each function call return a JSON object, with the following json schema:
{"name": "function_name", "arguments": <args-dict>}
Each function call should be enclosed within <tool_call> </tool_call> XML tags, only one tool per tag.
```

示例逐字包含 `run_command` / `write_to_file` / `codebase_search` 的完整参数 JSON。

**形态 B — 原生工具名标签**（直接以工具名为 XML 标签）：

```
# Examples
Example 1: Using a single tool
<run_command>
{"CommandLine":"npm test","Cwd":"/home/project/","Blocking":true,"WaitMsBeforeAsync":0,"explanation":"Running the test suite again after fixing the import issue."}
</run_command>
```

### 5.3 计划模式

`<PLAN>` … `</PLAN>` 段规定：先与用户对齐目标、产出 implementation plan artifact、拿到批准后才改代码；计划文件用固定小标题（`## [Goal Description]`、`## User Review Required`、`## Open Questions`、`## Proposed Changes`、`## Verification Plan`）。artifact 必须在 `{{ArtifactDirectoryPath}}` 下。

### 5.4 Artifact / 持久上下文 / 技能 / 插件

- **Artifacts**：`Artifacts are special markdown (.md) documents ...`，落到 `{{ArtifactDirectoryPath}}`；规定 GitHub alerts（`> [!NOTE]` 等五种）与 mermaid 图类型白名单。
- **Persistent Context**：KI（Knowledge Items）优先于 Conversation Logs，优先于重新调研；`{{KnowledgeDirectoryPath}}`、`{{SystemGeneratedLogsPath}}`、`{{ArtifactDirectoryPath}}`、`{{MetadataFileName}}` 是运行时注入变量。
- **Skills**：`SKILL.md` + `references/` + `scripts/`，渐进披露。
- **Plugins**：`plugins/<name>/` 内含 `plugin.json` / `mcp_config.json` / `hooks.json` / `rules/` / `skills/`。
- **MCP**：全局 `~/.gemini/config/mcp_config.json`，支持 stdio 与 Streamable HTTP 两种传输。

### 5.5 子代理与协作

```
## Invoking Subagents
Subagents can be invoked using the %[1]s tool. ... After launching a subagent, you do NOT need to poll or check your inbox in a loop.
## Communicating with Another Agent
Use the %[3]s tool to send a message to another agent by its conversation ID ... Do NOT use %[3]s to communicate with the user.
```

`send_message` 明确标注"不是跟用户对话的通道"。

二进制里另有一整套 Teamwork/swarm 编排提示词（`swarm_follower`、pipeline coordinator、tournament/verifier、`DeepCoder` 等），属于内部大规模多智能体实验，不一定在普通 CLI 路径上启用。

---

## 6. 工具集

`cortex/core/tools/tools` 包里 14 个一等工具结构体：

```
RunCommandTool  ReadFileTool   WriteFileTool  EditFileTool
ManageTaskTool  ManageSubagentsTool  InvokeSubagentTool  DefineSubagentTool
ScheduleTool    SearchWebTool  SendMessageTool  GenerateImageTool
SearchMarketplaceTool  RunWorkflowTool
```

每个都有 `Name()` / `Description()` / `ParameterSchema()`。参数描述以 Go 结构体 tag 形式内嵌（`jsonschema_description:"..."`），共提取到 175 条唯一描述，例如：

- `run_command`: `CommandLine` / `Cwd` / `BypassSandbox` / `Blocking` / `WaitMsBeforeAsync` / `RunPersistent` / `TerminalID` / `RunPersistentIndefinitely` / `toolAction` / `toolSummary`
- `write_to_file`: `TargetFile` / `CodeContent` / `Overwrite` / `Append` / `ArtifactMetadata`
- `edit_file` / `multi_replace_file_content`: `TargetFile` / `ReplacementChunks[]{StartLine,EndLine,TargetContent,ReplacementContent}` / `AllowMultiple`
- `invoke_subagent`: `TypeName` / `Prompt` / `Workspace`（`inherit`/`branch`/`share`）/ `ForkFromParent`

工具面还有一层更大的集合：`exa.cortex_pb.CortexStep*` 共 **148 种**步骤类型，涵盖 browser（点击/滚动/DOM/截图/控制台日志）、notebook、CloudSQL/Firebase 部署、`McpTool`、`InvokeSubagent`、`KnowledgeArtifacts` 等。内置工具的展示名（来自 `agy_tool_safety` 允许列表）比上面 14 个多，含 `view_file` `list_dir` `grep_search` `find_by_name` `search_in_file` `command_status` `read_url_content` `view_content_chunk` `task_boundary` `notify_user` `manage_inbox` `finish` `wait` `read_terminal` `generate_image` `trajectory_search` 以及一批 browser 工具。

---

## 7. 内嵌安全监控（`MonitorsConfig`）

二进制里有两份同构的 `monitors/monitor_config.proto` 文本（`# Checksum: sha256:...` 打头），三个监控项：

| name | mode | 触发阶段 | 作用 |
|---|---|---|---|
| `agy_tool_safety` | LOG | PRE_TOOL | 启发式：按 allowlist 判定工具调用安全与否 |
| `sir_flash_lite` | LOG | PRE_TOOL | 小模型预筛：决定这一步是否要送给更慢的 reviewer |
| `severe_internal_risks_agy` | **BLOCK** | PRE_TOOL | 重风险裁决，可阻断 |

`agy_tool_safety` 的 allowlist 是三张表：`allowed_tools`、`allowed_commands`（`ls` `pwd` `cat` `grep` …）、`allowed_subcommands`（`git log/diff/status/show/...`）、`allowed_tool_actions`（如 `manage_task` 只允许 `list`/`status`）。不在表里的一律进审查。

`sir_flash_lite` 与 `severe_internal_risks_agy` 是完整的评审 rubric，含风险信号 A–I、0–10 严重度分级、包含/排除规范与示例场景。两者都以 `REQUEST_TYPE_AGENT_MONITORING` 走同一套生成接口，是"旁路评审"模型调用。

对代理的含义：**这些监控是 CLI 自身发出的额外模型请求**，会被 `recordTrajectoryAnalytics` 之外的流量体现出来；`severe_internal_risks_agy` 的 `BLOCK` 模式能直接终止 agent 动作。做受限客户端工具执行时，这些是既有约束的一部分，不应绕过。

---

## 8. 与本仓库的对应关系

| 二进制里的东西 | 仓库里的对应 |
|---|---|
| `streamGenerateContent` 的 result 状态机 | `docs/headless-protocol-analysis-20261011.md` |
| `installation_id` + `jetski-standalone-oauth-token` | `docs/account-isolation.md`（多账号固定身份） |
| 工具声明只在会话首轮加载 | `docs/session-tool-configuration-fix-20261013.md` |
| `mcp_config.json` / skills / plugins 发现 | `docs/mcp.md`、`docs/skills.md` |
| `retrieveUserQuotaSummary` 的 bucket 结构 | `app/account_pool.py` 的耗尽判定 |
| `CLOUD_CODE_URL` 覆盖 | 可用于自建端点的改造点 |

---

## 9. 未确证与后续

1. **HTTP 头的逐字取值**：本文确认了头名（`X-Goog-Api-Key`、`Authorization: Bearer %s`、`X-Client-Protocol`、`X-HTTP-Session-Id`、`X-Jetski-*`）与请求体字段，但"这次请求到底带了哪几个头、UA 字符串长什么样"需要动态验证。静态字符串是被拼接的，无法还原 `%s/%s (%s)` 里各占位符的实参。
2. **`installation_id` 的生成算法**：确认是持久 UUID 文件，未确认是否与机器特征相关。`docs/account-isolation.md` 记录的是"每账号独立 UUID 并持久化"，不是派生。
3. **`user_prompt_id` 的来源**：字段存在，取值语义未确定。
4. **两种工具调用形态的选择条件**：取决于模型能力开关，具体判据在 `tool_calling_section` 配置里，未进一步定位。

验证建议：用 `CLOUD_CODE_URL` 指向本地 echo server，跑一次 headless 单轮，抓完整请求头与 body，即可把第 9.1 节补成[实测]。这比继续静态挖更省力，也不需要真实账号以外的东西。

---

## 附：提取产物

原始提取物在 `.devdata/agy-re/`：

| 文件 | 内容 |
|---|---|
| `chain_summary.json` | 端点、URL、scope、枚举、路径汇总 |
| `prompt_runs.txt` | 1,081 段可读提示词长文本 |
| `you_are_prompts.txt` | 114 段 `You are ...` 提示词 |
| `jsonschema_descriptions.txt` | 175 条工具参数描述 |
| `monitor_config_full.txt` | 完整监控配置 |
| `tool_calling_full.txt` | 工具调用提示词 |
| `strings_ascii_100.txt` | 全部 ≥100 字节 ASCII 串（22 MB） |
