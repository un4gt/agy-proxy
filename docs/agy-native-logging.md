# AGY 原生日志与日志级别

2026-10-14 在生产容器内核查 AGY **1.3.4**，并使用独立临时 HOME、空 stdin、无账号凭据进行启动探针。生产配置及在用会话未修改，未发送模型提示。探针因未登录退出 1，这是日志初始化验证，不是模型调用验收。

## 日志在哪里

| 类型 | 相对托管账号 HOME 的路径 / 来源 | 能分析的内容 |
|---|---|---|
| 原生 CLI / 后端日志 | `.gemini/antigravity-cli/log/cli-YYYYMMDD_HHMMSS.log` | 启动、配置/会话加载、认证/缓存/上游请求诊断、错误与部分内部耗时 |
| 完整会话记录 | `.gemini/antigravity-cli/brain/<conversation_id>/.system_generated/logs/transcript_full.jsonl` | 模型步骤、工具调用、token 数、checkpoint 等；含正文 |
| 普通会话记录 | 同目录 `transcript.jsonl` | 较精简的会话记录；仍含正文 |
| 流式事件 | CLI stdout 的 `init/step_update/result` | 步骤状态、`duration_seconds`、usage；代理消费此协议流 |
| stderr | CLI 错误/进度/权限提示 | 与文件日志不同；代理目前仅保留末尾 30 行供内部错误处理，DEBUG 日志只输出接收字符数 |

当前 `home` 测试账号的 HOME 是容器内：

```text
/root/.agy-proxy/accounts/7bf7a4e269ff493ead2eb6d59ba136aa/home
```

因此 AGY 原生日志不等同于 `docker logs agy-proxy`。Docker 日志主要是 Python 代理和 HTTP 服务的日志。原生文件在账号数据卷中；代理 Compose 的 Docker 日志轮转不会自动轮转这些原生文件。提高代理或 Uvicorn 的日志级别也不会改变 AGY 的 verbosity，且不会自动输出完整 stderr。

上次真实 DSH 测试的主会话：`4e015e0b-f3e4-48c3-b6c6-7e0290a1444c`。对应原生日志文件 `cli-20261013_155245.log`，约 113 KiB。原生 AGY 对照任务对应 `cli-20261013_160800.log`。

## 已有日志带来的新证据

主会话日志已有 47 条 `SEND_USER_CASCADE_MESSAGE_LATENCY`，包含：

- `load_trajectory_latency_ms`：加载会话轨迹。
- `resolve_agent_config_latency_ms`：解析 Agent 配置。
- `hydrate_slash_commands_latency_ms`：展开斜杠指令。
- `cascade_send_latency_ms`：提交给内部执行流程。
- `total_latency_ms`：本次消息提交阶段合计。

47 条记录的 `total_latency_ms` 合计 **218 ms**、中位 **4 ms**、最大 **32 ms**；其中加载轨迹合计 127 ms，内部提交合计 53 ms。**这些是消息提交阶段，不能拿 218 ms 代表整个 AGY 执行时间，也不包含随后的模型生成等待。** 它们不覆盖全部 48 个主请求的所有阶段，不能当作完整分区直接从先前的 531 秒中相减。

AGY 自身还有 profiler 机制，但这两份日志都明确写着：

```text
Continuous pprof profiling is disabled.
```

因此目前没有据此生成 AGY CPU 火焰图；持续 profiling 的启用方式尚未验证。先前对“代理未接入 CPU profiler”的描述只针对 Python 代理，不代表 AGY 内部没有 profiler 实现。

## 日志级别确实可以调整

| 参数 | 1.3.4 实测结果 |
|---|---|
| `--log-file /path/native.log` | 支持，帮助中有列出，探针确认创建指定文件 |
| `--v=2` | 支持，虽未列在 CLI 简版帮助中，实际增加详细日志 |
| `--vmodule=模块名=2` | 支持，按 Go 源文件模块选择 verbosity；逗号分隔多个模块 |
| `--debug`、`--verbose`、`--log-level debug` | 拒绝，退出 2，提示 flag 未定义 |

进一步做了定向验证：默认日志没有 `fs_logging_decorator` 的详细记录；`--v=2` 增加了 288 条文件系统记录和 95 条认证详细记录；`--vmodule=fs_logging_decorator=2` 同样增加 288 条文件系统记录，但认证详细记录仍为 0。证明模块过滤实际生效，而非仅被解析器接受。总行数受后台初始化与文件监听噪声影响，不用于性能倍数比较。

这是 Go 风格的 verbosity，详细记录仍可能以 `I` 开头，不应以是否出现 `DEBUG` 字样判断开关是否生效。[脱敏验证数据](reference/agy-native-log-evidence-20261014.json)保留了探针结果和原生耗时统计。

排查当前链路可先在隔离实例设置：

```dotenv
AGY_PROXY_BIN="agy --vmodule=http_helpers=2,conversation_manager=2,printmode=2,latency_breakdown=2"
```

这些模块已在真实日志中出现；能否提供模型首 token 或上游重试的额外计时，取决于模块内实际存在的 V 日志，不能预先保证。需要更广范围时，短时改为：

```dotenv
AGY_PROXY_BIN="agy --v=2"
```

代理已有命令行拆分与传参支持；本次补齐 `.env.example` 和 Compose 的 `AGY_PROXY_BIN` 映射。旧服务器 Compose 若未同步，单改 `.env` 不会传入此变量。源码运行时需要把它导出到启动进程环境；Compose 修改环境后需重新创建容器。现有 AGY 进程不会动态读取这些启动参数，需要新进程/新会话。

独立运行 CLI 时也可在原启动命令上追加 `--v=2 --log-file /tmp/agy-diagnostic.log`。并发进程应使用不同日志文件，不要让所有会话写同一个固定文件；默认的账号日志目录通常更便于按会话时间排查。

上述启动探针阶段没有在生产长期开启 verbose。全局 verbosity 会增加日志 I/O，原始日志及 transcript 可能包含路径、会话正文和认证诊断。性能比较应使用相同日志配置，分析产物只提取时间、步骤、计数等必要元数据。

## 后续生产诊断：2026-10-14

随后按用户要求在生产启用上述四个模块的定向 verbosity，并完成 [DSH 修改 Taskbox 的真实链路测试](archive/dsh-taskbox-followup-20261014.md)。本次只变更启动配置，生产镜像未更新。

实测发现同一秒启动的两个 AGY 进程会打开同名默认日志，使用独立非 append 偏移，造成内容覆盖。因此当前生产改用 [诊断启动脚本](../tools/agy_native_diagnostics.sh)：通过 `--log-file` 将每个进程写入账号 HOME 下的 `cli-diag-YYYYMMDD_HHMMSS-PID.log`，并保留定向 verbosity。Compose 设置 `AGY_PROXY_BIN: "/bin/sh /root/.agy-proxy/agy-native-diag.sh"`，脚本存放于持久化数据卷。

两个同秒并发进程已验证使用不同日志文件，真实请求、health、鉴权 models 均正常。日志参数当前保持开启；配置备份和回滚位置见任务报告。日志级别提升并未自动提供完整 DNS/TLS/首 token 分解；本次 Perfetto 文件是墙钟时间线，CPU pprof 仍未启用。

[返回链路耗时分析](latency-observability.md)
