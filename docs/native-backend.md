# 旧版原生后端（legacy）

main 只保留固定 SHA 1.4.2 native bridge。此前的 stream-json、GDB 原生观察器、`AGY_PROXY_NATIVE_CHAT`、独立 `Dockerfile.native` 与 `docker-compose.native-*` 已从 main 移出，完整实现和当时的部署说明保存在本地分支 `legay_stream_json_version`。

- 当前部署与客户端配置请阅读[部署指南](deployment.md)、[客户端接入](usage.md)和[环境变量](configuration.md)。
- main 的 bridge 使用 `GetChatMessageRequest`，不再启动 duplex stream-json 会话，也不再使用 GDB 观察器。
- 历史 release notes 与 archive 保留原样，仅用于追溯；其中的镜像标签、环境变量和验证命令不适用于 main。

需要复现旧版行为时，检出 `legay_stream_json_version` 分支并按其 Dockerfile/Compose 构建；不要把旧版环境变量混入 main 容器。
