# 原生实验后端

此后端仅接管 `/v1/chat/completions`。CLI 继续负责认证和上游 HTTP/TLS；GDB 读取响应并清理已验证的 CLI 输入包装，客户端负责执行工具、MCP 与 Skills。仍需等待完整 `finish` 参数与 CLI 终结结果一致后交付，尚未提供参数内逐 token 输出。

## 镜像与版本

使用 `tumuer/agy-proxy:v0.0.17-native.2`，可切换原公网入口，也可通过 [docker-compose.native.yml](../docker-compose.native.yml) 使用独立的 Compose 项目和数据卷。镜像内常规 CLI 为 1.4.2，原生后端单独固定到已验证的 Linux amd64 CLI 1.4.0；更新页面只更新常规 CLI，不替换原生二进制。

`native.1` 要求上游通过 `finish` 提交每个客户端回复，但真实长会话仍出现过裸文本输出。`native.2` 补充交付前的自动恢复：缺失 `finish` 时，最多在新的原生会话中按完整客户端历史重新生成一次，并沿用总超时、累计用量、校验后只交付一次。普通回答和工具请求均使用内部封装，客户端 `tool_choice=none` 仍然禁止客户端工具调用。详见[恢复机制与验证](releases/v0.0.17-native.2.md)。

## 使用原公网入口

在现有 Compose 项目中叠加 [docker-compose.native-primary.yml](../docker-compose.native-primary.yml)，可将原生 Chat 切到原服务端口并复用原账号、密钥和用量卷：

```sh
docker compose -f docker-compose.yml -f docker-compose.native-primary.yml pull
docker compose -f docker-compose.yml -f docker-compose.native-primary.yml up -d
```

升级前备份原 Compose、环境配置和两个数据卷。后续操作使用同样的两个 `-f` 参数；已有显式外部卷声明的部署继续沿用原 Compose。此覆盖配置提供原生后端所需的 GDB 权限，默认会话池容量为 2，可通过 `AGY_PROXY_NATIVE_MAX_SESSIONS` 调整。

Pi 的 Base URL 设为 `http://服务器IP:8317/v1`，API Key 使用原正式服务密钥，无需 SSH 隧道。Chat Completions 使用原生后端；Responses、Messages 和 Gemini 协议继续使用常规 CLI。完成验证后可停止原独立实验实例并保留其卷。日本节点已按此方式完成[公网入口切换](releases/native-primary-20261019.md)。

## 独立实验实例与 SSH 转发

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

公网部署将示例中的 Base URL 改为 `http://服务器IP:8317/v1`；独立实例的本地地址为 `http://127.0.0.1:18317/v1`。协议为 OpenAI Chat Completions。MCP 与 Skills 放在 Pi 客户端；不要为不同聊天设置同一个全局 `X-Agy-Session-Id`。

## 构建与版本固定

先构建普通镜像，再提供包含 `agy` 文件的独立构建上下文：

```sh
docker build -t tumuer/agy-proxy:v0.0.17 .
docker build -f Dockerfile.native --build-context native-cli=/path/to/verified-cli -t tumuer/agy-proxy:v0.0.17-native.2 .
```

原生二进制必须是 SHA-256 `60e72242f07c131f75afcd9a9e425d6ee097a1a1b3190ea98ae78fa4180bbfa8` 的 CLI 1.4.0。构建时校验哈希与版本并生成符号；本次发布使用此前验收容器保存的同哈希二进制。二进制不入 Git；没有此构建时可直接拉取发布镜像，不能用新版 CLI 替换后跳过校验。

实验镜像需要 `SYS_PTRACE` 和 `seccomp=unconfined`。每个会话有独立 CLI/GDB、二进制快照、管道和工作目录；原生池默认上限 2，其他协议仍使用常规池，两池容量分别计算。交付前候选结果错误共享一次自动恢复预算；采集不完整、结果不一致、非法原生工具调用仍直接失败。恢复、失败或取消后销毁相应会话。实验模式的账号删除会同时检查两个池。

## 验证范围

已验证 Pi 1.1.0 的内置工具、filesystem MCP、Skills，以及跨 owner 的相同 session ID 隔离和单边取消。输入清理只支持已验证的请求形态和固定 CLI。暂不支持图片；其他模型、远程 MCP OAuth、长时间负载、所有 HTTP 边界与升级兼容尚未完整覆盖。GDB 会影响时序。

v0.0.17 的本地和服务器验收都曾出现模型读完文件后提前结束、未完成后续工具任务，原样复测成功。此问题尚未修复，使用时仍需核实实际文件及检查结果；不能把进程退出码 0 当作任务完成。详见[发布验收记录](releases/v0.0.17.md)。

复验命令、原始实验限制见 [Pi 验收归档](archive/native-chat-pi-20261018.md)与[输入清理归档](archive/native-input-clean-20261018.md)。一般使用不要设置 `AGY_WIRE_INPUT_AUDIT`；它只用于会保存正文的受控审计。
