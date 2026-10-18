# 原生 CLI 输入清理验证

日期：2026-10-18。仅独立本地 Docker；未部署线上，未启动或修改原 `agyproto`。

## 已确认的问题

CLI 1.4.0 即使使用受限 `proxy-external` agent 和 `tools: [finish]`，实际向上游声明的工具仍为 `finish`、`manage_task`。实际系统提示在自定义 agent 文本后叠加服务端工作目录、后台消息、artifact 操作和文件链接规则。用户内容也包裹 CLI 时间和模型切换元数据。

`finish` 原描述要求“确认整个任务完成后调用”，与客户端需要逐轮提交工具请求的语义冲突。此前 pi 验收成功只能证明任务完成，不能证明这些叠加不存在。

## 实现与边界

新增默认关闭的 `AGY_PROXY_NATIVE_INPUT_CLEAN=true`，需同时启用 `AGY_PROXY_NATIVE_CHAT=true`。

- 在真实 CLI JSON 序列化返回处修改完整缓冲区，清理后以合法 JSON 空白补齐原长度，不调整 Go slice 指针、长度、容量；HTTP、TLS、认证和上游连接仍由 CLI 执行。
- 删除已确认的 CLI 环境、后台消息、artifact、沟通样式段和用户消息外层元数据；保留自定义桥接指令、客户端消息/工具配置、文档完整性 guidelines。
- 原生工具声明仅保留 `finish`，改为“提交下一轮客户端响应/工具请求”的描述；保留 CLI 要求的 toolAction/toolSummary 字段，但清除本地工具示例。
- 不修改外层路由/项目/身份字段、safetySettings、generationConfig 或执行权限。checkpoint 等辅助请求不清理。
- 每个会话拥有独立 CLI/GDB 和清理摘要集合。摘要覆盖完整请求，TLS 捕获必须与序列化后登记的摘要匹配，才接受该请求的响应；没有跨会话的 payload 映射表。
- 仅支持 SHA256 `60e72242f07c131f75afcd9a9e425d6ee097a1a1b3190ea98ae78fa4180bbfa8` 的 Linux CLI 1.4.0。未知版本在启动前拒绝；已命中的序列化请求若结构不符，会终止该 CLI。TLS 层发现未登记请求则拒绝交付响应，不能将这一事后校验描述为通用的发包前拦截。

这仍是实验后端：下游工具继续通过 `finish` 桥接执行，客户端 system/developer 消息仍保留在现有会话 JSON 中，并非已实现无桥接的原生工具协议。当前验证模型为 `gemini-3.8-flash-low`；其他模型、CLI 升级和其它请求结构需另行验证。工具模式仍等待完整 finish 校验，不宣称已实现参数内逐 token 交付。

## 真实验证

同一受控输入清理前后均回答 `AUDIT_OK`。上游报告输入 token 从 2962 降至 979（约 67%）；这是单样本提示量对比，不是延迟基准。为了保持内存布局，发送缓冲区长度并未减少。

pi 1.1.0：

| 场景 | 结果 |
|---|---|
| read / write / edit / bash | 随机 marker 与 JSON 总和匹配，独立 checker 通过 |
| 官方 filesystem MCP | 读取、写入、回读通过，随机 marker 匹配 |
| skills | 读取 SKILL.md 和相对引用，执行 checker 通过，skill/reference/checker 哈希不变 |

pi 三组使用独立客户端 session；MCP 与 skills 并发运行。TLS 受控审计共记录基准与 pi 请求 15 条，双 owner 会话测试请求 6 条；21 条均只声明 `finish`，无上述 CLI 环境段，客户端工具配置保留。

真实 Chat 路由额外验证：两个 API key 使用相同外部 session ID，各自随机 marker，交错并发两轮，CLI PID 和 conversation 均独立且可复用。取消 A 第三轮会销毁其 CLI/GDB；B 继续第三轮成功且保持原会话。退出测试后子进程全部回收。

离线回归覆盖清理边界、未知布局拒绝、辅助请求不变、安全/生成参数不变、客户端内容保留、会话隔离，以及已有执行和 headless 会话行为。证据摘要见 [JSON](../reference/native-input-clean-20261018.json)。本地原始受控证据在 `.devdata/native-input-20261018/`。

## 复验

```sh
uv run --with hpack pytest tests/test_native_input_clean.py tests/test_native_chat.py tests/test_native_wire_observe.py tests/test_native_wire_sessions.py tests/test_headless_sessions.py tests/test_unified_execution.py tests/test_session_isolation.py -q
# Linux 独立测试容器，先为匹配二进制重新生成 symbols.json：
python -m tools.agy_native_wire_probe.live_input_sessions
```

`AGY_WIRE_INPUT_AUDIT=/evidence/audit` 仅用于无敏感内容的受控 fixture。默认不启用；启用后会保存模型输入中的 systemInstruction/contents/tools，不保存请求头和外层账号/项目标识。不要在一般用户流量上开启。
