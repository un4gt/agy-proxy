# Pi 长会话无最终输出诊断（2026-10-13）

会话：`01a131b6-ec8f-74cf-82f7-84ce7e8a5b03`。本次仅进行日志分析和离线复现，没有修改生产实现、Pi 配置或部署，也没有重新执行历史浏览器操作。

后续修复：用户于 2026-10-13 明确要求移除代理字符上限，当前工作树已删除这一检查；下文保留故障发生时的配置和复现结果，文末的旧复现脚本对应修复前实现。当前行为见[配置文档](../configuration.md)及[发布说明](../unified-client-protocols.md)。

## 结论

直接原因是 **AGY 代理固定的 200,000 字符输入上限**。浏览器技能文档和页面快照不断进入历史，最后一次成功请求为 194,827 字符；追加工具结果后达到 211,062 字符，代理返回 HTTP 413。用户追问时历史仍超限，再次返回 413。

Pi 没有自动恢复有两个原因：配置的模型窗口是 1,048,576 token，距离它的主动压缩阈值还很远；代理的自定义错误文案和错误代码又没有命中 Pi 的上下文溢出检测。Pi 会话实际保存了错误，但用户反馈的界面只显示 `Turn completed`，未呈现失败原因。

已成功读取技能并执行大量浏览器工具。现有证据不支持“skills 工具定义或参数 Schema 适配失败”这一判断。技能文档和大快照是历史增长的来源，最终中断发生在代理输入检查阶段。

## 证据与时间线

本机实际运行的 Pi 包为 `@earendil-works/pi-coding-agent` 0.87.1，provider 为 `agy-proxy`，API 为 `openai-completions`。模型配置是 `gemini-3.8-flash`、thinking `high`；服务器记录确认 key 名为 `home`，解析后的模型为 `gemini-3.8-flash-high`。

服务器检查时镜像为 `agy-proxy:unified-20261012`，容器健康；配置为 `max_prompt_chars=200000`、`turn_timeout=600`、`recovery_attempts=1`。

以下时间为北京时间（UTC+8），行号来自原始 Pi JSONL：

| 时间或请求位置 | 事实 |
| --- | --- |
| 2026-10-13 04:11:19.401，第 23 行 | 用户发出本次 QA 任务 |
| 生成第 98 行响应之前 | 离线重建的请求为 194,827 字符，通过输入检查 |
| 第 99 行 | 浏览器工具返回 14,553 字符的结果 |
| 04:35:42.840，第 100 行 | 下一请求为 211,062 字符，收到 413，`content=[]`、`stopReason=error` |
| 第 101 行 | 用户追问“结果保存在哪里呢？” |
| 04:36:59.593，第 102 行 | 请求为 211,100 字符，再次收到同一 413，`content=[]`、`stopReason=error` |

两次错误完全一致：

```text
413: {"message":"Prompt and tool definitions too long for this proxy","type":"invalid_request_error","param":null,"code":"cli_protocol_error"}
```

这里测量的是经过 Chat 适配和消息规范化后的 JSON 字符数，不是模型 token 数。第一次失败请求原始序列化体积为 236,149 字节；本次没有图片附件。

服务器访问日志也有对应的两次 `/v1/chat/completions` 413。在 UTC `20:03:50–20:37:10` 窗口内，`home` 的请求计量记录有 46 条，全部为上述模型的成功请求。两个 413 没有进入这些计量记录。最后一条成功记录的关联 ID 为 `agy_1106534998a54f90885814b831408ef6`；不将它当作后续失败请求的 ID。

## 代理为何提前拒绝

相关源码位置（按本次诊断时工作树）：

- [app/config.py](../../app/config.py)：`Config.max_prompt_chars` 在第 67 行默认设为 `200_000`；`load_config()` 没有读取对应环境变量，因此只添加环境变量不能改变这个限制。
- [app/images.py](../../app/images.py)：`ImageInput.prepare()` 第 246–248 行序列化消息及工具等字段，并按字符长度返回 413。纯文本请求也经过此函数。
- [app/agy.py](../../app/agy.py)：`AgyError` 第 51–53 行的默认错误代码映射没有 413，因此本次落到 `cli_protocol_error`。
- [app/protocol_common.py](../../app/protocol_common.py)：`prepare()` 第 149 行执行输入检查，第 171 行才创建请求执行 profile，之后才构建 CLI 执行流程。
- [app/openai_api.py](../../app/openai_api.py)：第 161–165 行直接编码准备阶段的错误并返回，这一路径没有记录最终请求计量。

因此，最后两个失败请求没有进入 AGY CLI 或模型推理，也未产生本轮业务流内容。已有一次恢复机制用于符合条件的执行失败，不能修复这个前置输入长度错误。24 分钟也不是某一次 CLI 推理超出了 600 秒超时。

## Pi 为何没有压缩

使用本机已安装 Pi 的实际函数离线验证，结果如下：

| 项目 | 值 |
| --- | --- |
| 模型配置 `contextWindow` | 1,048,576 token |
| 默认压缩 `reserveTokens` | 16,384 token |
| 主动压缩阈值 | 1,032,192 token |
| 最后一次成功响应的 usage 总量 | 161,220 token |
| 第一次失败后，Pi 根据最后 usage 加后续消息估算 | 164,859 token，`shouldCompact=false` |
| 第二次失败后同样估算 | 164,862 token，`shouldCompact=false` |
| 对实际错误执行 `isContextOverflow` | 两次均为 `false` |
| 对标准长度错误样例执行相同检测 | 两次均为 `true` |
| 会话 compaction 记录 | 0 |

最后成功响应的 usage 为 input 0、output 350、cacheRead 160,870。上述数字是 Pi 采用的计量或估算，不能拿来与代理的字符预算直接等同。

Pi 的 `pi-ai/dist/utils/overflow.js` 支持 `/prompt (?:is )?too long/i` 和 `context_length_exceeded` 等模式。当前文案在 `Prompt` 与 `too long` 之间插入 `and tool definitions`，错误代码又是 `cli_protocol_error`，均未匹配。Pi 不会把所有 HTTP 413 都当成上下文溢出。

检测对照样例为 `Prompt is too long for this proxy` 搭配 `context_length_exceeded`。这只证明 Pi 能识别该错误；没有验证压缩请求能否通过相同代理长度限制，不能据此宣称仅修改文案就能完成端到端恢复。

## 时间花在哪里，skills 是否有效

QA 请求到首次失败共 **1,463.439 秒，即 24 分 23.439 秒**。其中 38 次工具调用从 assistant 发起到 toolResult 返回的间隔合计 **1,243.026 秒，约 85%**。这些间隔包含命令启动、浏览器交互和等待等；现有日志不能进一步精确区分各部分开销。

第 60 行的工具调用耗时 329.268 秒，命令链是三次 `npx agent-browser` 执行 `click → wait 500 → snapshot -i`。显式等待只有 500 毫秒，不能将整个耗时解释成模型思考或主动等待。没有子命令级计时，无法确定这 329 秒具体卡在哪一步。

全会话有 43 次工具结果（`read` 1 次，`bash` 42 次），累计 171,023 字符。较大的内容包括：

- 第 15 行技能 core 文档：37,980 字符，读取成功。
- 第 95 行页面结果：14,447 字符。
- 第 99 行最后一次页面结果：14,553 字符。

5 次已标记的工具错误分别是：1 次本地找不到 `agent-browser` 命令、3 次页面元素引用失效、1 次点击目标被弹窗遮挡。首次命令不存在后，历史改用 `npx agent-browser` 并成功读取 core；这些都是命令或页面操作层面的错误，没有出现工具参数缺字段等契约校验错误。

大量重复页面快照放大了历史，但合理的长任务不应因此只呈现无解释的结束状态。需要修复代理预算、错误兼容和界面反馈，缩短工具结果仅是辅助优化。

## 结果保存在哪里

没有找到这次任务的最终 QA 报告。会话无 `write`/`edit` 调用；42 次 bash 均为浏览器命令。发现的 shell 重定向都是 `2>/dev/null`，没有报告文件输出。检查测试工作目录时只存在 `.agents`、`.claude`、`skills-lock.json`。

中间观察和操作结果仍在原始会话中：

```text
C:\Users\MT308\.pi\agent\sessions\--E--RUST_WORKSPACE-emrank-tech-test--\2026-10-12T20-03-54-127Z_01a131b6-ec8f-74cf-82f7-84ce7e8a5b03.jsonl
```

会话停止时仍在请求日志页面，未见完成设置页检查和最终汇总的记录。浏览器保存的内部状态不等于用户要求的测试报告。

## 界面状态的证据边界

Pi JSONL 明确保存了两条错误，而用户反馈界面仅显示 `Turn completed`。这只能表示回合结束，不能据此判断任务成功。

本机 `C:\Users\MT308\.pi\agent\extensions\pebrel.ts` 的第 45–48 行在 `agent_end` 时直接发送 `done`，没有检查错误状态。这是一条可能的显示链路；没有取得截图对应 GUI 的渲染实现及运行时事件，尚不能把它认定为唯一原因。

## 修复建议及验证范围

1. **协调代理与客户端的输入预算。** 将字符预算真正接入配置，并基于 AGY CLI 的实测能力设定限制；区分代理保护限制与模型 token 窗口。不要静默截断历史。当前不能仅凭模型标称百万窗口就认为整条链路支持同等容量。
2. **返回可识别的长度错误。** 对这条输入超限分支提供稳定错误代码、Pi 可识别的文案、实际长度、上限和关联 ID。不要把图片数量等所有 413 都改成上下文溢出。压缩恢复必须实测摘要请求和后续请求均能通过限制。
3. **记录前置失败并展示错误。** 准备阶段拒绝也应有一条明确的最终请求诊断，未知用量不要伪造为成功计量；Pi 前端应显示错误原因，区分失败与成功结束。
4. **改善长任务输出。** 浏览器快照尽量限定区域，阶段性保存观察及未完成项，降低历史增长和全部成果依赖最后一条回复的风险。

后续验收应覆盖：接近上限后追加大工具结果、超限后的压缩及继续、压缩本身超限、界面错误呈现、前置失败计量。既有短会话兼容验收无法覆盖本次暴露的容量边界。

## 本次验证与产物

本机离线重建使用 Pi 实际 OpenAI 序列化入口的 `onPayload` 截获请求，禁止网络 fetch；结果为 **0 次网络调用**。随后使用本地 Chat 适配器和 `ImageInput.prepare()` 验证三组历史，结果依次为通过、413、413。重建输入不包含图片附件，没有启动 CLI 或重新执行浏览器工具。

脱敏统计见 [证据 JSON](../reference/pi-empty-output-evidence-20261013.json)。包含历史正文的重建请求和本机复现脚本仅在被 Git 忽略的 `.devdata/pi-empty-output-20261013/` 中，不纳入报告或版本提交。

本机复现入口（依赖本机原始会话及已安装 Pi）：

```powershell
node .devdata/pi-empty-output-20261013/reconstruct.mjs
.\.venv\Scripts\python.exe -c "import runpy; runpy.run_path('.devdata/pi-empty-output-20261013/reproduce_proxy.py')"
```

本次没有改动实现，因此未重跑全量测试套件；完成的是实际会话的离线边界复现、Pi 压缩判定对照和日志交叉核对。
