# 链路耗时分析

## 当前服务器与本次变更

2026-10-14 实际检查生产容器 `agy-proxy`：镜像为 `agy-proxy:no-prompt-cap-20261013`。已有 `agy turn started/init/result` 日志（PID、会话 ID、轮次和耗时），以及 `execution` 日志（`request_id`、协议、尝试次数、修复次数、结果）。使用 `sudo docker logs agy-proxy` 查看。日志驱动是 `json-file`，容器没有显式轮转参数。

未发现 `py-spy` 可执行文件；容器有 `opentelemetry-api 1.45.1`，未发现 `opentelemetry-sdk`，应用源码没有 OpenTelemetry 或 CPU profiler 接入。现有日志不能直接展开端到端时间线，也不能把旧 `think` 字段解释成模型纯推理时间。

后续核查确认 AGY 本身另有原生日志、会话 transcript，以及可用的 `--v`/`--vmodule` 详细日志开关；还存在默认关闭的持续 pprof 机制。见[AGY 原生日志与日志级别](agy-native-logging.md)，其中包含实际文件位置和新增的消息提交耗时证据。

本次在工作区补齐结构化阶段计时、追踪导出脚本和 Compose 日志轮转；**尚未部署生产**。此前“工具配置每会话只加载一次”的修复也仍只在隔离实例验证。本文的真实任务图使用已有隔离测试原始数据，未重新发起完整编码基准，也不代表生产已获得同样优化。

## 已生成的真实任务时间线

- [可直接查看的耗时图](reference/dsh-agy-latency-timeline-20261014.png)
- [Chrome Trace / Perfetto 追踪文件](reference/dsh-agy-latency-trace-20261014.json)
- [完整测试报告与限制](archive/dsh-agy-latency-rerun-20261014.md)

在 [Perfetto](https://ui.perfetto.dev/) 中选择 **Open trace file**，打开 JSON，展开 DSH HTTP、AGY、客户端工具等轨道。点击请求查看 `request_id`；框选放大两段约 120 秒的工具等待。辅助标题请求使用独立轨道，不叠加到主请求关键路径。原生 AGY 是另一次独立运行，在图中以自身启动时间归零。

这是一份**墙钟时间线，不是 CPU 火焰图**。客户端时间来自同一个单调时钟；服务器与客户端的时钟偏移由全部 49 个请求的发送/完成边界约束，交集宽度约 66.44 毫秒，使用交集中点对齐，位置误差界约 ±33.22 毫秒。各段持续时间保持原始测量值。对齐假设是观察期间相对时钟偏移近似固定、网络耗时非负，不能用它推断精确单向网络延迟。

| 主链路阶段 | 总耗时 | 优化含义 |
|---|---:|---|
| AGY 执行 | 531.44 秒 | 含 CLI 内部处理和上游等待；不能仅凭这个总数拆成推理、预填充或压缩 |
| DSH 客户端工具等待 | 298.20 秒 | 两次单测各约 120 秒后转后台，优先处理 Windows 沙箱临时目录权限问题 |
| 网络、SSH、relay 残差 | 9.87 秒 | 含多段传输及本地中继，非纯网络 RTT |
| 服务器其余处理与交付 | 4.56 秒 | 包含前置、会话获取、校验等；不是独立 CPU 用量 |
| 客户端启动、调度、退出 | 7.65 秒 | DSH 剩余开销 |

扣除 HTTP 与工具区间约 3.89 毫秒重叠，合计 851.71 秒；原生任务为 442.02 秒。同一源码在相同 Windows/Python 下脱离 DSH 沙箱运行，21 项单测约 8.92 秒通过；沙箱内 `tempfile.mkdtemp` 后读目录/清理出现 WinError 5，而普通 `mkdir` 可用。首次单测后的剩余时间分别为约 475 秒和 81 秒，说明修复客户端测试环境是更直接的优先项。它对本次 409.69 秒总差距的因果贡献仍需修复后复测，不能简单相减就承诺提速。

本次 DSH 有 48 次生成，原生为 34 次。下一步应在相同工具执行环境下比较轮数、上下文大小与 AGY 每轮等待，再决定是否调整思考级别或工具策略。保持当前 `high` 可以避免把模型行为变化混进基础设施优化。网络和 Python 校验并不是本次主耗时；完整工具校验合计所在的 `CLI result → ready` 区间只有约 54 毫秒，没有理由为加速取消校验。

## 新日志的用法与含义

更新到包含本次改动的版本后，每个推理 HTTP 请求结束时输出一条 `agy_proxy.latency: request_latency {...}`，成功、流内错误、超时、取消和发送失败均覆盖。用响应头 `X-Agy-Request-Id` 与客户端关联。GET 健康检查、控制台访问和历史检索不输出该计时记录。

```bash
# 服务器：只导出需要分析的时间段
sudo docker logs --since 30m agy-proxy > proxy.log 2>&1

# 有源码的本机或服务器：只需 Python 标准库
python tools/export_latency.py --log proxy.log --output proxy-trace.json

# 或仅展开一个请求
python tools/export_latency.py --log proxy.log --request-id agy_实际请求ID --output request-trace.json
```

旧版本日志不能补造缺少的阶段，脚本会明确报没有匹配记录。导出支持普通日志、JSONL 和 Docker `json-file` 包装；默认按请求独立轨道，支持同一服务器上的并发请求。不直接合并多个未同步时钟的服务器。

| 阶段 / 时间点 | 定义 |
|---|---|
| `prepare` | 共享请求校验、图片准备、契约建立及模型选择；不含进入共享适配器前的鉴权/HTTP 解析，后者可在 HTTP 起点到 prepare 的间隔看出 |
| `models_lookup` | 实际查询/缓存读取模型列表；是 prepare 的子区间 |
| `lease_lock_wait` | 领取请求级租约时等管理器锁；并发同会话仍返回 409，无新增队列 |
| `session_acquire` | 会话选择、必要时回收及启动；包含 `session_lock_wait`、`process_start` |
| `process_start` | 私有工作目录配置及创建 CLI 进程；进程创建完成不表示模型已经初始化 |
| `attempt` | 每次尝试独立记录序号、PID 和是否为新会话，可与原有 CLI 日志关联 |
| `prompt_build` | 构建本轮输入，记录输入字符数及固定会话配置字符数；配置长度不表示再次发送了配置 |
| `cli_turn` | 从开始调用 CLI 到收到 result，错误/取消时记录至退出；不包含收到 result 后的候选校验 |
| `attempt_N_init` / `attempt_N_first_step` | 首个 init/step 被代理看到的时间；step 可能是工具或内部输出，不是模型首 token |
| `validate` | 完整候选/参数/图片检查及结果清理；失败与成功分开记录 |
| `recovery_restart` | 故障后的进程回收/重建，记录触发原因；后续生成属于下一个 attempt |
| `validated_result` | 已通过检查、准备交给协议编码器；不代表客户端已收到 |
| `session_release` | 释放会话/必要时杀进程，含相关锁等待 |
| `headers_sent` / `first_body_sent` | ASGI send 返回的时间；第一个 body 可能只是开始事件或心跳 |
| `first_business_sent` | 首个已验证内容的 ASGI send 返回时间；不包括心跳，仍不是客户端确认接收 |
| `response_body_complete` | 最终 ASGI body 发送返回；`total_ms` 还包括后续收尾和计量 |

这些区间有父子关系，**不能把所有 span 相加当总耗时**。单次请求内部使用单调时钟，开始时间另记 Unix 微秒以便按时间排列。每请求最多保留 64 个阶段、64 个时间点，超过时记录丢弃数量，不会逐 token 输出日志。新增记录不包含请求正文、模型正文、脚本、参数、工具结果、异常消息或凭据。没有新增追踪服务、后台上传或额外模型调用。

Compose 设置每个日志文件 20 MB、保留 5 份；重新创建容器后生效。使用其他部署方式时，沿用其日志采集与轮转设置。`X-Agy-Profile`/流末注释改为实际阶段汇总；详细记录以请求结束日志为准，响应头本身不可能包含响应发送完成后的时间。

## 什么时候需要 CPU 火焰图

如果新日志显示 `prepare`、`prompt_build`、`validate` 或进程启动明显变慢，再对代理 Python 进程采样。例如在装有 py-spy 且允许 ptrace 的隔离实例运行：

```bash
py-spy record --pid <代理Python进程PID> --duration 30 --rate 49 --format speedscope -o python-cpu.json
```

默认不加 `--idle`，避免把等待线程误当 CPU 热点。输出可用 speedscope 打开。这只能解释代理进程的栈，不能展开远端模型推理，也不能代替 DSH 客户端工具计时。当前没有生成或声称生成这种 CPU 采样图。

## 验证

新增测试覆盖四种协议 × 流式/非流式、首次参数错误后恢复、恢复耗尽、总超时、ASGI 头/正文发送失败、取消、输入错误、日志正文脱敏及三种日志封装的追踪导出。全量测试：529 passed，2 skipped（Windows 不运行 Linux PTY 测试）。另已核对 366 个追踪事件中的 48 个主请求、48 个工具区间与原始耗时一致，服务器区间均位于对应客户端 HTTP 区间内。图表源文件含 SHA-256，追踪导出未包含原始脚本/工具参数。

重建现有基准图（原始证据保存在本地 `.preview`，图表生成额外需要 matplotlib；不加入应用依赖）：

```bash
uv run --no-project --with matplotlib python tools/export_latency.py --benchmark .preview/dsh-latency-rerun-20261013 --output docs/reference/dsh-agy-latency-trace-20261014.json --chart docs/reference/dsh-agy-latency-timeline-20261014.png
```
