# DSH 修改 Taskbox：服务器原生日志与链路耗时实测

2026-10-14 使用真实 DSH，在上次 Taskbox 的副本中完成依赖调度、稳定拓扑排序和事务批量导入。**总耗时 1,034.924 秒（17 分 14.924 秒）；42 项单测和 37 项独立黑盒检查全部通过。** 客户端到服务器的网络、SSH 与两端计时转发残差合计 **4.233 秒**，无法解释主要等待；长等待位于 AGY 执行、原生压缩和恢复后的模型回合。

这次按要求调整了生产 AGY 的日志启动参数，**生产镜像仍为 `agy-proxy:no-prompt-cap-20261013`，没有部署当前工作区的代理代码**。线上旧版仍每轮追加工具配置；本次不能视为“工具配置仅加载一次”修复版的性能测试，也不能与上次从零开发的任务直接计算提速倍数。

## 实际任务与产物

来源为 `.preview/dsh-latency-rerun-20261013/valid/dsh-work`，复制到 `.preview/dsh-taskbox-followup-20261014/dsh-work`；原件和初始 SHA-256 保留。DSH 修改了 `taskbox.py`、`test_taskbox.py`、`README.md`，实现：

- `depends_on`、图完整性校验、依赖替换/清空、完成及删除约束。
- `list --ready/--blocked`、最小 ID 优先的稳定拓扑排序。
- 支持前向引用的批量导入、dry-run、失败不写入且不消耗 ID、一次原子提交。
- 21 项新增单测和可运行文档示例。

DSH 使用已从源码安装和构建的 `deepseek-harness`，提交 `d743267388641bc76f17c45ce8b4c231aed1d32c`，版本 `0.2.1-alpha.2`；启动方式为 `pnpm dsh --profile headless --json -`，没有使用 npx。任务通过 stdin 完整传入，首次工具请求验证了任务文本一致性。模型为 `gemini-3.8-flash-high`、reasoning `high`，DSH 重试为 0，代理恢复预算为 1；工具在 Windows、Python 3.12.7、`workspace-write` 沙箱中运行。

DSH 实际执行：原有 21 项单测 7.835 秒通过；修改主程序后 21 项 5.668 秒通过；最终 42 项 10.836 秒通过。独立重跑为 **42 项、12.173 秒通过**；另外的 **37 项 CLI 黑盒检查全部通过**。保留了全部 21 个原测试方法；其中原 JSON Schema 检查增加了 `depends_on` 断言。没有人工代写生成项目的代码。

## 时间分解

链路为 DSH → Windows 计时 relay → SSH → 容器计时 relay → 原生产代理 → AGY。两个 relay 只记录时点、转发原始载荷；添加请求编号头用于对齐。各侧使用本机单调时钟计算持续时间，不用跨机器时钟差计算单程网络延迟。

| 阶段 | 秒 | 占总时间 |
| --- | ---: | ---: |
| AGY 日志记录的 turn 执行，包含生成、上游等待及压缩 | 977.570 | 94.46% |
| 服务器其他阶段，包含未计入 turn 的进程重建等 | 14.240 | 1.38% |
| 客户端到服务器的网络、SSH、两端 relay 残差 | 4.233 | 0.41% |
| DSH 本地工具，按并行区间并集 | 28.923 | 2.79% |
| 客户端启动、轮间调度和退出 | 9.959 | 0.96% |
| 总计 | **1,034.924** | **100%** |

主流程 20 次 HTTP 请求、21 次 CLI 生成尝试，另有一次并行标题请求（16.401 秒），不重复加到关键路径。23 次客户端工具调用中一次返回版本过期错误；没有本地 120 秒测试挂起。主 HTTP 合计 996.043 秒，服务器 relay 合计 991.810 秒。CLI 日志的 turn 持续时间精度为 0.01 秒；剩余 14.240 秒不是 Python CPU 时间，不能用它推断 Python 处理很慢。

测试前复用连接 health 往返 145–202 毫秒，测试后 141–171 毫秒，全部 HTTP 200；冷连接分别约 351、280 毫秒。这是含 SSH 和转发的完整往返，不是纯线路 RTT。正式请求的响应头延迟中位数 287 毫秒，首业务内容延迟中位数 15.291 秒。

**这些测量排除了本地到服务器传输是主要原因，但未独立测出服务器到 Google 上游的 DNS、TCP、TLS、排队和首 token 时间。** 这些等待仍包含在 AGY 区间中，不把整个区间称为纯模型计算时间。

## 本次定位的长等待

| HTTP 请求号 | 客户端耗时 | 实际行为与证据 |
| --- | ---: | --- |
| 5 | 198.650 秒 | 最终仅更新待办；返回用量含 30,553 reasoning tokens；服务器耗时 198.405 秒 |
| 6 | 127.945 秒 | 生成主程序；写入被 DSH 的文件版本检查拒绝 |
| 7–8 | 64.957 秒 | 重读主程序后重新生成并成功写入；本地工具自身耗时很短 |
| 13 | 124.206 秒 | 生成新增测试；返回用量含 8,797 reasoning tokens |
| 14 | 180.477 秒 | 原生压缩后出现缺字段调用；代理拦截，使用一次恢复后交付合法调用 |
| 15–17 | 198.620 秒 | 恢复后执行 git status、再次读取 README 和测试；共 30,209 reasoning tokens |

返回的主流程 output tokens 合计 128,095，其中 reasoning 91,753。这是 CLI 返回并由代理累计的用量；没有单独拿到 CHECKPOINT 内部模型消耗，不以此声称完整覆盖所有 AGY 内部计算。

### 原生压缩和恢复

初始会话最大观察输入为 **196,785 tokens**，由未缓存输入加缓存读取计算。随后出现明确的原生 `CHECKPOINT`：创建时间 `07:28:30Z`，下一 `PLANNER_RESPONSE` 开始于 `07:30:40Z`，故压缩阶段约 **130 秒**，时间戳精度为秒。它是 Agent 历史摘要/检查点行为，不是证明 Gemini 的模型 context window 只有 20 万，也不是单纯滑动丢弃最旧 tokens。

压缩后的候选 `pwsh` 缺少必填 `description`。代理在交付前记录 `tool_arguments_invalid`，校验器为 Draft202012Validator，执行一次恢复。失败调用没有交给 DSH；修复候选改为合法的 `read README.md`，随后任务继续。

该请求初次 AGY turn 为 149.24 秒，修复 turn 为 25.72 秒，另有约 4.5 秒修复进程初始化及其他阶段。整轮客户端 180.477 秒，不能再把压缩或修复时间重复加到总时间。执行记录为 `attempts=2, repair_attempts=1, outcome=success`。下一正常请求另建进程，其启动和后续生成也已经计入总耗时。

### 修复时的长历史被 AGY 原生截断

修复会话的第一条 USER_INPUT 只有 191,988 字符，带 `<truncated 86712 bytes>`；恢复后的正常新会话第一条输入同样为 191,988 字符，带 `<truncated 96346 bytes>`。这表明完整客户端历史拼成单条输入后，**AGY 自身进行了单条输入截断**。此处字符与 token 是不同单位；它也不是已经删除的代理 200,000 字符检查。

随后出现多轮重新读取文件和较长 reasoning。可以确定截断与这些回合在同一恢复链路内；仅凭日志不能量化其中多少秒由截断直接造成。后续应验证不触发该原生截断的历史恢复方式，而不是在代理里再添加字符上限或静默删内容。

### 线上旧版仍重复工具配置

本次主链路三个 conversation 中的 21 条 USER_INPUT 均含一个 `[Client tool configuration]` 块。客户端每轮提交 25 个工具，工具定义序列化长度为 22,407 字符（此数是客户端请求中声明的长度，不是整个 AGY 桥接提示长度）。固定配置不断进入历史的现象仍存在。

本地已有“配置按会话加载一次”的修复，并完成过隔离测试，但本次只调整服务器日志参数，未上线该修复。应先解决这一已知重复，再以相同任务复测；不能通过提高模型名义上下文上限消除 AGY 的检查点或单条输入限制。

### DSH 的文件版本检查

首次写 `taskbox.py` 返回 `FS_STALE_VERSION`。DSH 的版本比较包含文件身份、大小、mtime 和 ctime，不能简单认为内容确实被另一人改写。当时独立 SHA-256 仍与初始文件一致；重读后写入成功。没有捕获拒绝当刻的两份版本元组，因此不将根因归结为 ACL 或特定程序。该错误与重新生成带来的约 65 秒已计入本次总耗时。

## 原生日志发现及服务器最终配置

首次重建后实际 AGY 参数包含：

```text
--vmodule=http_helpers=2,conversation_manager=2,printmode=2,latency_breakdown=2
```

发现默认原生日志文件名只有秒精度。主请求 PID 95 和标题请求 PID 100 在同一秒启动，打开了同一个 `cli-20261014_071826.log` inode，文件描述符不是 append 模式，写入位置不同。采集过程中可解析的消息提交记录数量曾减少，符合内容被覆盖的现象。**这份原生日志不完整，不能据它断言没有上游重试。** 代理计时日志和按 conversation 分开的 transcript 不受这个文件冲突影响。

测试完成后部署了 [诊断启动脚本](../../tools/agy_native_diagnostics.sh)，最终 Compose 配置为：

```yaml
AGY_PROXY_BIN: "/bin/sh /root/.agy-proxy/agy-native-diag.sh"
```

脚本在持久化数据卷中，保留上述定向 verbosity，通过 `--log-file` 使用账号 HOME 下的 `cli-diag-YYYYMMDD_HHMMSS-PID.log`，并保持所有原有启动参数。没有更换生产镜像。

并发真实验证的两个进程在同一秒启动，分别写入 `cli-diag-20261014_074041-40.log`、`cli-diag-20261014_074041-48.log`；实际 cmdline、文件描述符和文件存在性均验证。两个 Chat 请求返回 HTTP 200 且内容正确，health 和鉴权 models 也为 200。

服务器 Compose：`/home/docker_depolyments/agy-proxy/docker-compose.yml`。恢复本次诊断调整前的配置可使用备份 `docker-compose.yml.before-native-diag-20261014T071432Z`；仅恢复到无 PID 日志版本的备份为 `docker-compose.yml.before-unique-native-log-20261014T073915Z`。还原配置后需重新创建容器。当前定向日志保持开启；本次 DSH、relay 和 SSH 测试进程已结束。

定向原生日志仍含可能敏感的请求诊断；共享证据仅提取元数据。本次没有启用 AGY CPU pprof，因此提供的是**墙钟时间线，不是 CPU 火焰图**。

## 证据与复现

- [脱敏结构化证据](../reference/dsh-taskbox-followup-evidence-20261014.json)：请求配对、耗时、usage、工具区间、修复、检查点、截断标记、测试结果和日志启动验证。
- [Perfetto 时间线](../reference/dsh-taskbox-followup-trace-20261014.json)：在 Perfetto 打开 JSON，可展开主 HTTP、标题请求、本地工具、AGY 主会话、修复和检查点。跨主机时间戳对齐仅用于展示；持续时间统计来自各侧自己的时钟。
- 本地项目：`.preview/dsh-taskbox-followup-20261014/dsh-work/`；原始副本：同目录 `before/`；修改 diff：`taskbox-changes.patch`。
- 复核脚本：同目录 `verify.py`、`acceptance.py`、`analyze.py`；任务原文 `task.txt`；DSH session `session-5c8a5b8e-3551-43be-98a7-b7dbf1582240`。
- 最初 SSH 密钥选择错误导致的 9.236 秒准备失败保留在 `excluded-ssh-setup/`：未向生产发起请求、未修改项目，不混入正式样本。

最直接的优化顺序是：先消除线上重复配置，再修复会话重建中的长历史截断兼容，随后以同一任务比较 reasoning effort 和模型的小步骤行为。服务器到上游的网络细分仍需要额外可观测性；当前证据不足以把它和模型推理完全拆开。
