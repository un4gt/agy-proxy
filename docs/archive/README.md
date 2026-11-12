# 历史研究与验收归档

本目录保留各次实验当时的实现、版本、证据与限制，不代表当前部署状态。机器摘要仍位于 `../reference/`，原始敏感证据不入库。2026-11-11 起 main 只保留 native bridge；文中指向 `tools/agy_native_wire_probe/`、`app/native_chat.py`、`app/execution.py`、`Dockerfile.native`、`docker-compose.native-*` 和旧测试文件的链接，对应实现已移到 `legay_stream_json_version` 分支。

当前操作入口：[部署指南](../deployment.md)、[客户端接入](../usage.md)、[Bridge 后端与旧版实现](../native-backend.md)、[v0.0.17 发布说明](../releases/v0.0.17.md)。

| 主题 | 记录 |
|---|---|
| 原生后端最新验收 | [输入清理](native-input-clean-20261018.md)、[Pi 工具/MCP/Skills](native-chat-pi-20261018.md) |
| 原生传输研究 | [TLS 传输](cli-native-transport-validation-20261018.md)、[并发会话](cli-native-sessions-validation-20261018.md)、[二进制静态分析](agy-cli-binary-analysis-20261016.md) |
| 协议与会话 | [Headless](headless-protocol-analysis-20261011.md)、[会话连续性](session-continuity-and-todos-20261011.md)、[配置去重](session-tool-configuration-fix-20261013.md)、[提示长度](prompt-cap-removal-20261013.md) |
| 客户端验收 | [服务器与 DSH](dsh-server-acceptance-20261012.md)、[Pi Taskbox](pi-taskbox-mcp-skills-20261014.md)、[DSH Taskbox](dsh-taskbox-followup-20261014.md) |
| 性能与故障分析 | [DSH 延迟](dsh-agy-latency-analysis-20261013.md)、[延迟复测](dsh-agy-latency-rerun-20261014.md)、[链路耗时与日志字段](latency-observability-20261014.md)、[AGY 原生日志（1.3.4）](agy-native-logging-1.3.4.md)、[Pi 空输出](pi-empty-output-analysis-20261013.md)、[Bash 参数](bash-invalid-arguments-analysis-20261012.md) |
| 发布整理 | [native.10 已发布变更与验证范围](released-v0.0.17-native.10.md) |
| 其他方案调研 | [CLIProxyAPI 兼容性](cliproxyapi-antigravity-compatibility-research-20261012.md) |
