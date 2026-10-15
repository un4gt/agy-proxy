# bash 参数校验失败联合分析（2026-10-12）

## 结论

已通过服务器请求记录、AGY conversation 数据库和实际运行版本的校验代码定位并复现。第 22 次模型调用返回了 `bash(arguments={"cmd":"…"})`，而客户端声明的 bash Schema 要求 `command`、`description` 两个必填字符串。代理因此拒绝向客户端交付这条调用。

**直接原因是模型生成的参数字段名与客户端契约不一致，并缺少必填字段；不是 JSON 引号或换行转义失败。** 失败输出的 JSON 和桥接外层 Schema 均合法。失败命令为 9,141 字符，包含 `cat … << …` heredoc，改用 heredoc 不能修复这次参数字段错误。

本次为分析与只读取证，未修改运行代码、服务器配置或部署。未执行数据库中的客户端命令，也未将工作内容或原始数据库导出到仓库。脱敏证据见 [证据 JSON](reference/bash-invalid-arguments-evidence-20261012.json)。

## 事件对应关系

用户补充的账号名称与服务器 API Key 名称一致；时间按 Asia/Shanghai（UTC+8）核对。服务器使用 `tumuer/agy-proxy:v0.0.15`，本地 HEAD 为 `01f7156`。`external_tools.py`、`openai_api.py`、`agy.py` 的服务器与本地 SHA-256 完全一致。

匹配到的 AGY conversation 为 `9f3c01cc-8dad-4af1-81af-d1726fc7c105`，PID `7583`，`owner_session=b8a469903b27`，按客户端历史连续复用（`affinity=history`）。实际运行模型为 **`gemini-3.8-flash-low`**；用户给出的 `agy-proxy/gemini-3.8-flash` 是客户端配置名称，不能直接当作最终执行变体。

| 北京时间 | 服务器证据 |
|---|---|
| 15:34:37.104 | 工具桥接 AGY 第 1 轮开始 |
| 15:34:46–15:37:48 | 同一 PID、conversation 的前 21 轮成功 |
| 15:37:49.894 | 第 22 轮开始；HTTP SSE 已返回 200 |
| 15:38:43.355 | AGY 返回 `SUCCESS`，上游耗时 53.46 秒 |
| 15:38:44.680 | 工具桥接记录 `status=aborted` |
| 15:38:44.681292 | 控制台记录 `ERROR 502`，总耗时 54,790.7 ms，usage 各项为 0 |

客户端一个 turn 内可以包含多次模型调用，这里的第 22 轮是代理/AGY 模型调用计数，不是客户端新开了 22 个用户 turn。15:34:36 附近还存在另一个不走工具桥接的请求，未将其混入这条调用链。

## 参数级取证与复现

以 SQLite `mode=ro` 读取目标 conversation 数据库，按 protobuf 字段提取 user-input 中的 `[Client tool configuration]` 及 finish 对外结构化结果。只输出 Schema 结构、字段名、长度、哈希和校验结果；不输出脚本正文、用户工作内容或模型内部推理。

22 次输入携带的客户端工具配置完全相同，排除了本轮客户端突然更改工具定义这一解释。bash Schema 的关键部分为：

```json
{
  "type": "object",
  "properties": {
    "command": {"type": "string"},
    "description": {"type": "string"}
  },
  "required": ["command", "description"]
}
```

实际 Schema 还允许 `justification`、`run_in_background`、`sandbox_permissions`、`timeoutMs`、`workdir`；没有禁止附加属性。因此 `cmd` 的出现本身不是 `additionalProperties` 违规，真正失败的是缺少 `command` 与 `description`。

| 检查 | 前 21 次 | 第 22 次 |
|---|---|---|
| 公开 finish 结果 JSON 可解析 | 全部通过 | 通过 |
| 工具桥接外层 Schema | 全部通过 | 通过 |
| 按客户端工具 Schema 复验 | 全部通过 | 失败 |
| 第 20、21、22 次 bash 参数字段 | 第 20、21 次均为 `command`、`description` | 只有 `cmd` |

数据库第 66 条 step 的公开 finish 输出 SHA-256：

```text
da007e73e498ee5a7093d5cad3520084b8cd8e3a650e7b6fcb312bd9e9f30bb4
```

将该输出交给服务器现有 `ExternalTools.result()`，精确复现：

```text
agy returned invalid arguments for client tool bash
```

独立用同一 Schema 的 validator 枚举错误，得到两个 `required` 错误，缺少字段分别为 `command` 和 `description`。整个过程仅校验内存中的数据，没有再次请求模型或执行 bash。

另外，本地用不含用户内容的合成命令验证四种组合：

| 命令写法 | 参数只有 `cmd` | 参数为 `command` + `description` |
|---|---|---|
| `python3 -c` | 精确复现相同错误 | 通过 |
| heredoc，含引号与多行文本 | 精确复现相同错误 | 通过 |

这验证了错误取决于参数契约，而非这两种 shell 写法。

## 代理为什么返回这类错误

1. [ExternalTools.schema](../app/external_tools.py#L186) 给 AGY 的 finish Schema 只约束 `arguments` 是对象，并不按不同工具嵌入各自参数 Schema。客户端完整定义放在提示词中，由模型遵循。
2. AGY 的 finish 可以接受 `{"cmd":"…"}` 这个合法对象并返回 `SUCCESS`。
3. [ExternalTools.result](../app/external_tools.py#L273) 再用客户端原始 Schema 检查 bash 参数。在第 296 行失败，经第 298 行转为通用 `AgyError`；其默认内部状态是 502。
4. [ExternalTools.run](../app/external_tools.py#L306) 在校验前缓冲结果，校验失败不会发出客户端工具调用。失败会话被销毁，避免继续复用未交付的隐藏状态。
5. [OpenAI SSE 错误分支](../app/openai_api.py#L343) 在 HTTP 200 已发出后发送错误事件：

```json
{"error":{"message":"agy returned invalid arguments for client tool bash","type":"api_error"}}
```

随后发送 `[DONE]`。这个 SSE 对象没有数字状态或稳定的错误 `code`。**控制台的 `ERROR 502` 不等于客户端收到 HTTP 502。** 因此仅依赖 HTTP 状态的 5xx 分类无法覆盖这条流内错误路径。

6. 代理 [account_failover](../app/agy.py#L56) 只处理特定配额耗尽异常，不会重试这类参数校验失败；也没有让模型修正参数的恢复流程。

本次未取得该用户安装版本的 DSH 适配器代码或原始客户端 session，故不能独立证明其 `classifyPiAiError()` 的全部映射与重试规则。用户提供的 `PI_AI_ERROR` 和停止行为，与服务器的 HTTP 200 + SSE `api_error` 路径相符。

## 对原分析的校正

| 原说法 | 联合分析结论 |
|---|---|
| 错误文案来自代理 | 已证实，精确定位并重现 |
| 第 22 次工具未交付客户端 | 服务器校验与缓冲逻辑支持；未取得客户端原始记录独立复核 |
| 引号或裸换行破坏 JSON | 本次不成立：公开 JSON 可解析且外层校验通过；缺少必填字段才是直接原因 |
| 全部使用 `python3 -c`，改 heredoc 可根治 | 已交付步骤不能代表失败步骤；失败命令已包含 cat heredoc，不能根治字段错配 |
| usage 全 0 说明模型未正常生成 | 不成立：AGY 返回 SUCCESS；代理错误分支传入 `usage=None`，统计层补零，不能据此断言真实消耗 |
| 55 秒说明生成了一大段内容后解析失败 | 时长本身不能证明；数据库证明确有约 9 KB 命令字符串，失败发生在参数 Schema 校验阶段 |
| 第 20 步 `(no output)` 是故障先兆 | 没有因果证据。第 20、21 次输出均通过同一工具参数校验，空 stdout 可以是正常执行结果 |
| TCP 可连通能说明这次问题性质 | 当前连通性不能解释历史故障；本次已有具体结果与参数错误证据，无需以网络不稳定解释 |

## 建议与证据边界

优先在代理层改进：

1. **补充脱敏诊断。** 记录工具名、错误类别、validator、参数路径、缺失字段、conversation/request 关联标识；避免输出 `ValidationError` 的完整字符串，因为其中可能包含脚本和用户数据。目前内层校验捕获所有 Exception，并抹掉了具体失败原因，应区分数据校验错误与引用解析等异常。
2. **加入受限的模型修正机会。** 对确认属于参数 Schema 的失败，在尚未交付任何客户端工具调用、且没有不可重放副作用的条件下，反馈精确缺失字段和工具 Schema，最多修正一次并重新完整校验。不要自动猜测把所有 `cmd` 改名成 `command`，也不要替客户端静默填造必填字段。
3. **评估生成端约束。** 若 AGY finish 支持，可让工具名与对应参数 Schema 关联；需要覆盖多工具、本地 `$ref` 和 CLI 支持范围。即便增强生成端约束，交付前的客户端 Schema 校验仍须保留。
4. **改善流内错误语义与计量。** 增加稳定的错误代码及诊断关联标识，针对 DSH 实际 SDK 验证可重试分类；单纯更换错误文字或宣称内部 502 不够。上游已完成但输出被拒时，可单独记录已知用量并保留失败状态，避免展示为零消耗。

用户侧可以提醒模型“严格使用工具声明的字段名并填写必填字段”，但这只是降低概率。heredoc 对 shell 可读性有帮助，不能保证工具参数合法。

已确定的是模型在第 22 次输出中发生参数字段错配；**为什么这次改用了 `cmd` 尚无充分证据**，不能据此进一步归咎于上下文压缩、提示词冲突或特定采样设置。本次证据也不足以统计跨用户发生率，或证明所有工具校验错误都来自同一原因。
