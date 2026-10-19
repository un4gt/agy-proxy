# 2026-10-19 — 原生 Chat 切换至原公网入口

日本节点原 `8317` 服务已改用 `tumuer/agy-proxy:v0.0.17-native.1`，镜像摘要 `sha256:1d7adeed561f6cbf3582a4aa0b742562de33966de51e6356424e9c66f47c2fad`。Chat Completions 使用已修复 `finish` 契约的原生后端，Responses、Messages、Gemini 协议使用常规 CLI。

客户端使用原公网 Base URL `http://服务器IP:8317/v1`。Pi 的现有 `agy-proxy` provider 已从本地 SSH 转发地址改为公网地址，API Key 和模型配置保留，修改前已备份本机配置。已运行的客户端重新加载配置后即可直接访问。

## 持久化与部署

- 保留正式服务原 `agy-proxy_agy-data`、`agy-proxy_agy-gemini` 两个卷。
- 核对 API Key 的 ID／密钥／名称／创建时间、累计模型请求数、两个隔离账号的 installation ID，均保留。
- 新镜像具备 GDB 及固定 AGY 1.4.0 原生二进制；常规 CLI 为 1.4.2。添加 `SYS_PTRACE` 和 `seccomp=unconfined`，默认会话池容量设为 2。
- 旧独立 `agy-native` 实验实例已停止，`18317` 不再作为使用入口；其数据卷、凭据和历史记录仍保留，未与正式数据合并。
- 仓库提供 [docker-compose.native-primary.yml](../../docker-compose.native-primary.yml) 覆盖配置；线上原 Compose 已直接更新镜像、权限及相关环境变量，之后在原目录执行 `docker compose up -d` 即可维持本次部署。

## 验证

公网四协议 × 流式／非流式共 8 组真实工具请求、结果回填与最终回答全部通过。镜像内回归与多轮原生验收沿用[同一镜像的修复记录](v0.0.17-native.1.md)。切换后的真实 Pi 验收及部署信息记录于[脱敏验收结果](../reference/native-primary-validation-20261019.json)。

## 备份与回滚

切换前停服完成一致备份：`/home/docker_depolyments/agy-proxy/backups/native-primary-20261019/`，包含原 Compose、环境配置、完整两个卷的副本、镜像身份及保留数据校验记录。旧镜像另标记为 `agy-proxy:rollback-native-primary-20261019`。

如需切回常规 Chat 后端，保持现有数据卷，在正式部署目录执行：

```sh
cd /home/docker_depolyments/agy-proxy
sudo cp backups/native-primary-20261019/docker-compose.yml docker-compose.yml
sudo docker compose up -d --no-build --pull never agy-proxy
```

不覆盖升级后的数据和用量；原始卷副本仅用于额外数据恢复。回滚后公网地址和 API Key 仍相同。
