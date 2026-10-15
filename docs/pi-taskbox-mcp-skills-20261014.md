# Pi + 三个 MCP + 两个 skills：Taskbox 真实编码实测

2026-10-14 使用本机 **Pi 0.87.1**、`gemini-3.8-flash-high`、thinking `high`，通过生产代理完成和上次 DSH 相同的 Taskbox 复杂修改。**607.915 秒（10 分 7.915 秒）结束并输出最终答复；43 项单测、37 项独立 CLI 黑盒检查全部通过。** 三个 MCP 均有成功调用，两个 skills 均实际读取。没有人工代写项目代码。

本次主要等待仍在 AGY 执行区间；本机到服务器网络、SSH 和计时 relay 的残差合计 3.266 秒，无法解释长等待。AGY 区间包含上游网络、生成及内部处理，不能将其全部称为模型计算。

## 环境和任务

隔离目录 `.preview/pi-taskbox-mcp-skills-20261014/`，项目为 `pi-work/`，初始副本为 `before/`。从上次 DSH 的 **初始项目**复制，三个源文件的 SHA-256 完全一致；没有从 DSH 已完成的结果继续修改。原始任务文本相同，额外要求实际使用 MCP 和 skills。

Pi 新增依赖图校验、依赖替换、完成/删除约束、ready/blocked 筛选、最小 ID 优先拓扑排序、支持前向引用的事务导入及 dry-run。修改 `taskbox.py`、`test_taskbox.py`、`README.md`，新增 22 个测试方法，保留全部 21 个原方法；原 JSON 输出断言增加 `depends_on` 字段。

实际 Pi session 为 `01a139e8-dfb9-727a-90ac-39feddcc454e`。主任务从 UTC `10:15:25.016601` 到 `10:25:32.931467`。Pi 退出码 0，最终 assistant `stopReason=stop`；不是只看进程退出码判断成功。

- 使用本机 `pi.cmd`；隔离 `PI_CODING_AGENT_DIR`，禁止自动重试，代理恢复预算 1；使用已有 `home` key，证据不包含密钥。
- 本地固定安装 `pi-mcp-adapter@5.3.0`，filesystem、memory、sequential-thinking MCP server 均为 `1.0.0`。包锁保留在 `deps/`；直接使用本地 Node 入口，没有使用 npx。
- 三个 MCP 使用 stdio，filesystem 只允许测试项目；memory 数据单独存储；配置为 exclusive，不导入用户全局 MCP。
- 两个技能为 `dependency-graphs`、`transactional-cli-tests`，保存在测试目录 `skills/`。按 skill-creator 编写并通过 `quick_validate.py`，提供图不变量和事务测试方法，没有提供任务实现。
- 生产仍是 `agy-proxy:no-prompt-cap-20261013`。保留上一轮启用的 AGY 定向日志和带 PID 的日志文件名；本轮没有部署本地代理改动。

主样本开始前已经做过 MCP 连接探测，适配器 metadata cache 已预热；时间不包含安装和构建成本。Pi 模型声明 contextWindow 为 1,048,576，maxTokens 为 32,768；这是客户端声明，不能用它推断 AGY 内部压缩阈值。

## 独立验证

Pi 自己先运行原 21 项测试，完成实现后再次通过 21 项；新增测试后通过 43 项，收尾再次通过完整套件。随后独立重跑 **43 项、12.714 秒、OK**；37 项黑盒检查覆盖前向引用、稳定排序、旧数据、依赖操作、失败字节不变、ID 不消耗、dry-run 和损坏图拒绝，全部通过。

项目保留 `WorkspaceTempDir`。测试进程没有遗留；独立验证和短 MCP 探针的时间均不计入主任务。

## 链路耗时

Pi → 本机透明计时 relay → SSH → 容器计时 relay → 生产代理 → AGY。两端 relay 原样转发业务载荷，额外请求编号只用于配对。持续时间分别使用本机单调时钟；跨机器时钟不用于计算单程网络耗时。

| 阶段 | 秒 | 占总时间 |
| --- | ---: | ---: |
| AGY 日志记录的 turn 执行 | 557.800 | 91.76% |
| 服务器其他阶段 | 1.283 | 0.21% |
| 本机到服务器网络、SSH、两端 relay 残差 | 3.266 | 0.54% |
| Pi 本地工具和 MCP，按执行区间并集 | 43.496 | 7.15% |
| Pi 启动、轮间调度、退出 | 2.070 | 0.34% |
| 总计 | **607.915** | **100%** |

15 次 HTTP 请求对应 15 次 AGY 生成，没有代理恢复、没有原生 CHECKPOINT、没有观察到输入截断标记。27 次本地工具调用中 1 次 MCP 内层参数错误，后续模型回合纠正成功。工具耗时主要是执行测试；全部通用 MCP 调用的单次持续时间之和为 2.064 秒，两个 skill 读取之和 1.885 秒，这两项有并行，不能直接加到关键路径。

响应头延迟中位数 256 毫秒，首业务内容中位数 12.625 秒。测试前 health 复用连接约 174–237 毫秒，测试后约 173–200 毫秒，全部 200；冷连接分别约 259、288 毫秒。此值含 SSH 和 relay，不是纯线路 RTT。

| 请求 | 客户端耗时 | 行为与返回用量 |
| --- | ---: | --- |
| 5 | 65.142 秒 | 写入 memory 设计记录；7,836 reasoning tokens |
| 6 | **267.667 秒** | 生成完整主程序；33,471 reasoning tokens |
| 8 | 92.176 秒 | 生成完整测试文件；1,988 reasoning tokens，另有较多代码输出 |
| 12 | 20.218 秒 | 写入 README |
| 15 | 16.432 秒 | 最终答复 |

第 6 轮服务器耗时 267.471 秒，与客户端仅相差 196 毫秒；期间有 26 个 SSE 心跳。心跳只说明连接存活，不是模型首 token，也不保证推理有进展。受限工具模式在完整候选校验后才交付业务内容，因此长代码生成在客户端呈现为等待后一次交付。

返回 output tokens 合计 75,285，其中 reasoning 47,771。最大观察输入为 180,439 tokens（未缓存输入加缓存读取）。15 次原生消息提交日志合计仅 44 毫秒、单次 0–8 毫秒；该指标不是模型响应时间。定向 HTTP 日志能确认上游端点调用，但没有足够的明确阶段语义，不能据日志行间隔可靠拆分 Google 上游 DNS/TCP/TLS、排队、首 token 和生成时间。

## MCP 和 skills 的实际结果

两个 skill 文件通过 `read` 成功读取。三个 MCP 的业务调用成功如下：

| MCP | 成功业务调用 |
| --- | --- |
| filesystem | `read_text_file` 4 次，读取源码和最终 README |
| sequential-thinking | `sequentialthinking` 1 次，设计/不变量检查 |
| memory | `create_entities`、`add_observations`、`open_nodes` 各 1 次，保存并回读决策与测试结果 |

主样本使用通用 `mcp` 调用入口。最初 CLI 工具白名单写了 `filesystem_*` 等通配符，但 **Pi 0.87.1 没有按预期展开它们**：直接工具已经注册，却未激活。实际 HTTP 仅声明 `read`、`write`、`edit`、`powershell`、`mcp` 共 5 个工具，定义序列化长度 6,436 字符。发现后没有中途换配置，避免混成两个样本。

第 3 轮 `sequential_sequentialthinking` 将可选字段 `branchFromThought`、`branchId`、`revisesThought` 填为 null，而内层工具要求 integer/string。MCP 适配器拒绝执行，错误回传后第 4 轮纠正成功。那一轮同时包含其他读取工作，因此不能把整轮 9.203 秒都算成错误额外成本。

**这次不是 skills 载入失败，也不是代理忽略了已知 Schema。** 通用 `mcp` 只对外声明工具名和内层参数载体；代理看不到具体 MCP 的内层契约。对同一错误候选做离线回放，通用外层契约校验通过，直接工具契约立即返回 `tool_arguments_invalid`，准确指向这三个字段的类型约束。回放使用本地代理校验代码，没有执行客户端操作，也没有故意让线上模型产生错误。

直接暴露 Schema 的真实模型探针也已通过。使用精确工具名白名单：

```text
--tools filesystem_read_text_file,sequential_sequentialthinking,memory_create_entities,memory_open_nodes
```

实际 HTTP 收到这四个独立工具及完整参数 Schema，定义共 6,157 字符。四个工具各成功执行一次，最终正确返回 `PI_DIRECT_MCP_OK`；没有工具错误，四个请求均为一次生成、零代理修复。无模型 RPC 探针另验证了所配置的 10 个直接 MCP 工具都可以激活。

这个短探针没有加载 skills，但用时 **258.090 秒**，其中第一轮仅请求读取小文件就花 **220.638 秒**，返回 28,961 reasoning tokens（总 output 29,027）。后续三轮分别为 18.939、8.951、6.848 秒。因此“短工具任务也可能出现长 high reasoning”得到另一个真实样本支持，不能将长等待统归因于 skills 或 MCP 本地执行。单次探针不足以证明直接 Schema 导致或改善了推理时长。

探针证据在主 JSON 的 `direct_mcp_probe` 字段及隔离目录 `direct-probe/`；主任务统计不包含它。离线错误回放证明直接 Schema 能校验这类错误，不代表线上探针实际触发了一次修复。

对于此前 `description` 缺失，只有客户端声明为必填字段时代理才能按契约校验并在交付前恢复；上次 DSH 测试已经验证这一行为。不能从本次通用 MCP 内层错误推断代理会漏掉已声明必填字段，也不应从字段名推断它一定可以安全自动补值。

## 与 DSH 的对照及优化方向

| 项目 | 上次 DSH | 本次 Pi + MCP/skills |
| --- | ---: | ---: |
| 总耗时 | 1,034.924 秒 | 607.915 秒 |
| AGY turn 合计 | 977.570 秒 | 557.800 秒 |
| 主 HTTP 请求数 | 20 | 15 |
| reasoning tokens | 91,753 | 47,771 |
| 原生压缩 | 1 次，约 130 秒 | 未观察到 |
| 代理修复 | 1 次 | 0 次 |
| 最终单测 / 独立黑盒 | 42 / 37 | 43 / 37 |

这次样本比上次少约 427 秒，但不能归因为 Pi 或 skills 提速：工具集、系统提示、沙箱、集成要求、模型生成路径均不同，DSH 另有文件版本拒绝和压缩恢复。本轮也没有增加相同任务的原生 AGY CLI 对照，不能据此量化代理相对直连的净开销。

线上旧版仍将工具配置追加进每轮 USER_INPUT，15 轮都出现一个配置块。本地“会话只加载一次”修复尚未上线。应先验证并部署这一修复，再以固定任务、工具集、effort 做对照；同时采用直接 MCP Schema 以提前校验，减少错误往返。较长的 high reasoning、整文件输出，以及恢复时 AGY 原生长输入截断也是后续需要分别测量的因素。

## 证据和复现材料

- [脱敏 JSON 证据](reference/pi-taskbox-mcp-skills-evidence-20261014.json)：两端请求配对、用量、工具成功/失败、skills、原生日志元数据、测试与文件哈希。
- [Perfetto 时间线](reference/pi-taskbox-mcp-skills-trace-20261014.json)：HTTP、本地工具和 AGY 回合；这是墙钟时间线，不是 CPU 火焰图。
- 本地 `.preview/pi-taskbox-mcp-skills-20261014/`：`run_pi.py`、`analyze.py`、`verify.py`、`acceptance.py`、`replay_schema.py`、任务原文、包锁、skills、项目和修改 diff。
- 主样本最终服务器快照保存在该目录 `server/`，在直接探针开始前采集，不混入探针的请求和日志。
- 准备阶段有一次本地载荷完整性断言错误，2.187 秒、没有请求到生产、没有修改项目；证据保存在 `excluded-payload-assertion/`，未混入主样本。另一次 RPC 探测仅发生输出解析错误，未发模型请求。

共享证据不包含 key、认证头、完整工具参数或业务正文。原始私密请求/SSE/客户端事件留在隔离目录用于复核，不应当作脱敏报告直接发布。

测试结束后已停止本次 SSH 隧道和容器 relay，确认没有本次 Pi、runner 或 MCP 子进程遗留；生产 health 返回 200。AGY 定向日志继续保留，生产镜像和代理逻辑没有变更。
