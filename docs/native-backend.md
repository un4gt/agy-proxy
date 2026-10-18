# 原生实验后端

此后端仅接管 `/v1/chat/completions`。CLI 继续负责认证和上游 HTTP/TLS；GDB 读取响应并清理已验证的 CLI 输入包装，客户端负责执行工具、MCP 与 Skills。仍需等待完整 `finish` 参数与 CLI 终结结果一致后交付，尚未提供参数内逐 token 输出。

## 独立部署

使用 `tumuer/agy-proxy:v0.0.17-native` 和仓库的 [docker-compose.native.yml](../docker-compose.native.yml)，与正式服务使用独立的 Compose 项目和数据卷。镜像内常规 CLI 为 1.4.2，原生后端单独固定到已验证的 Linux amd64 CLI 1.4.0；更新页面只更新常规 CLI，不替换原生二进制。

```sh
# 在独立目录放置 docker-compose.native.yml 与 .env，设置控制台密码。
docker compose -p agy-native -f docker-compose.native.yml up -d
curl http://127.0.0.1:18317/health
```

健康响应应显示 `version: 0.0.17`、`chat_backend: native`、`native_input_clean: true`。默认仅绑定服务器回环地址，远程体验通过 SSH 转发：

```sh
ssh -N -L 18317:127.0.0.1:18317 <服务器 SSH 别名>
```

随后打开 `http://127.0.0.1:18317`，添加账号、创建客户端 API Key。需要从现有服务迁移时，在源服务停止且备份完成后，将凭据与数据复制到独立卷；两个运行实例不能共用可写数据目录。复制账号保留其安装标识，副本后的用量与密钥管理独立。

## Pi 接入

将 [models.json](../examples/pi-native/models.json) 放入独立 Pi 配置目录，设置：

```powershell
$env:PI_CODING_AGENT_DIR = '<独立 Pi 配置目录>'
$env:AGY_NATIVE_API_KEY = '<实验入口的客户端密钥>'
pi --provider agy-native --model gemini-3.8-flash-low --thinking off
```

Base URL 为 `http://127.0.0.1:18317/v1`，协议为 OpenAI Chat Completions。MCP 与 Skills 放在 Pi 客户端；不要为不同聊天设置同一个全局 `X-Agy-Session-Id`。

## 构建与版本固定

先构建普通镜像，再提供包含 `agy` 文件的独立构建上下文：

```sh
docker build -t tumuer/agy-proxy:v0.0.17 .
docker build -f Dockerfile.native --build-context native-cli=/path/to/verified-cli -t tumuer/agy-proxy:v0.0.17-native .
```

原生二进制必须是 SHA-256 `60e72242f07c131f75afcd9a9e425d6ee097a1a1b3190ea98ae78fa4180bbfa8` 的 CLI 1.4.0。构建时校验哈希与版本并生成符号；本次发布使用此前验收容器保存的同哈希二进制。二进制不入 Git；没有此构建时可直接拉取发布镜像，不能用新版 CLI 替换后跳过校验。

实验镜像需要 `SYS_PTRACE` 和 `seccomp=unconfined`。每个会话有独立 CLI/GDB、二进制快照、管道和工作目录；原生池默认上限 2，其他协议仍使用常规池，两池容量分别计算。自动恢复关闭，失败或取消销毁相应会话。实验模式的账号删除会同时检查两个池。

## 验证范围

已验证 Pi 1.1.0 的内置工具、filesystem MCP、Skills，以及跨 owner 的相同 session ID 隔离和单边取消。输入清理只支持已验证的请求形态和固定 CLI。暂不支持图片；其他模型、远程 MCP OAuth、长时间负载、所有 HTTP 边界与升级兼容尚未完整覆盖。GDB 会影响时序。

复验命令、原始实验限制见 [Pi 验收归档](archive/native-chat-pi-20261018.md)与[输入清理归档](archive/native-input-clean-20261018.md)。一般使用不要设置 `AGY_WIRE_INPUT_AUDIT`；它只用于会保存正文的受控审计。
