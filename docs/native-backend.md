# Bridge 后端与旧版原生实现

main 只保留固定 SHA 1.4.2 bridge。此前的 stream-json、GDB 原生观察器、`AGY_PROXY_NATIVE_CHAT`、独立 `Dockerfile.native` 与 `docker-compose.native-*` 已从 main 移出，完整实现和当时的部署说明保存在本地分支 `legay_stream_json_version`。

> [!WARNING]
> **agy CLI 版本锁定：** bridge 只支持固定 SHA 的 1.4.2 构建。不要升级 CLI、执行 `agy update` 或用其他版本替换 `/opt/agy-bridge/agy`；升级需要重新生成补丁并完成完整回归，步骤见[部署指南](deployment.md#cli-版本与更新)。

- 当前部署与客户端配置请阅读[部署指南](deployment.md)、[客户端接入](usage.md)和[环境变量](configuration.md)。
- main 的 bridge 使用 `GetChatMessageRequest`，不再启动 duplex stream-json 会话，也不再使用 GDB 观察器。
- 历史 release notes 与 archive 保留原样，仅用于追溯；其中的镜像标签、环境变量和验证命令不适用于 main。

需要复现旧版行为时，检出 `legay_stream_json_version` 分支并按其 Dockerfile/Compose 构建；不要把旧版环境变量混入 main 容器。
