# 原生实验后端

此后端仅接管 `/v1/chat/completions`。CLI 继续负责认证和上游 HTTP/TLS；GDB 读取响应并清理已验证的 CLI 输入包装，客户端负责执行工具、MCP 与 Skills。仍需等待完整 `finish` 参数与 CLI 终结结果一致后交付，尚未提供参数内逐 token 输出。

## Token 用量来源

原生后端复用 HTTP/1、HTTP/2 观察器已解析的 `usageMetadata`，不使用 CLI `result.usage` 或其累计差值记账。每个上游 agent 请求按请求 ID 保留最新用量快照，再汇总本轮请求；重复的 SSE 快照不重复计数，其他轮次和旁路请求不混入。

存储沿用原有口径：输入为 `promptTokenCount - cachedContentTokenCount`，缓存单列，输出为 `candidatesTokenCount + thoughtsTokenCount`，总量为 `totalTokenCount - cachedContentTokenCount`。OpenAI 响应通过已有转换函数恢复包含缓存的输入和总量，思考 Token 不重复相加。

每个请求都捕获到最终用量且响应完整结束后，才标记 `usage_complete=true`。缺失或异常用量不会回退到 CLI 值；保留已知消耗并标记不完整。502、超时、取消及修复重试也保留已捕获的用量，各次尝试只累计一次。常规 CLI 后端和账号剩余额度查询的来源不变。

## 镜像与版本

使用 `tumuer/agy-proxy:v0.0.17-native.10`，可切换原公网入口，也可通过 [docker-compose.native.yml](../docker-compose.native.yml) 使用独立的 Compose 项目和数据卷。原生后端单独固定到已验证的 Linux amd64 CLI 1.4.0；更新页面只更新常规 CLI，不替换原生二进制。

`native.8` 接入图片附件：保持输入清理开启，只允许 `view_file` 读取请求专属目录中的附件，逐一确认读取成功后再验证最终 `finish`。单图、多图、流式响应、图片会话续接及 Pi 工具返回图片格式均通过真实验证。仍按完整结果校验后交付；格式、数量和大小限制沿用既有图片输入校验。详见[发布记录](releases/v0.0.17-native.8.md)。

`native.9` 修正上游错误识别顺序：先检查 HTTP 状态，再解析错误正文，成功响应才按 SSE/JSON 提取候选内容。上游将 429 JSON 错误标为 `text/event-stream` 时，仍正确返回 `quota_exhausted` 和重置时间；普通限流不会标记账号额度耗尽。已经开始的客户端 SSE 流通过错误事件报告失败。详见[发布记录](releases/v0.0.17-native.9.md)。

`native.4` 防止 CLI 的单片段 192,000 字节截尾破坏大段历史或工具结果；在固定版本下保留完整代理输入，继续严格校验包装及输出。详见[根因、边界与验证](releases/v0.0.17-native.4.md)。

`native.7` 修复连接状态复用、HTTP/2 分片前言和缺失 finish 的完成判定，并为交付前的捕获不完整提供一次新进程恢复；Token 用量改用原生响应。上述行为由 native.8 继承。详见[发布记录](releases/v0.0.17-native.7.md)。

`native.5` 的轮次/FIFO 和模型目录修复、`native.6` 的基础模型额度分组一并包含。

`native.10` 发布 finish 轮次边界修正：GDB 在固定 CLI 内限制 finish 检查、强制提交计数与结构化结果选择的作用范围，防止历史 finish 被算作当前轮提交。缺失提交时由同一 CLI executor 在本轮内部补交，最多三次；原生提交次数耗尽时返回 `native_finish_exhausted`，保留用量且不再整轮重放。构建时重新生成符号文件。native.8 和 native.9 均不包含这项边界修正。本版也包含控制台布局、加载状态和账号额度分组更新，以及重新构建的前端产物；范围与验证见[发布记录](releases/v0.0.17-native.10.md)。

已验证的 Cloud Code 路由在 `ANY + finish` 下仍可能返回纯文本。不能把设置 ANY 等同于上游已强制执行输出契约；轮次边界修正与上游约束失效是两个不同问题。内部补交仍会增加模型调用与等待，不承诺持续缺失时也能成功。

`native.3` 修正 OpenAI / Responses / Gemini 对缓存输入的计数转换，详见[用量口径与验证](releases/v0.0.17-native.3.md)。这项修复不改变上游缓存策略。

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
docker build -f Dockerfile.native --build-context native-cli=/path/to/verified-cli -t agy-proxy:native-dev .
```

原生二进制必须是 SHA-256 `60e72242f07c131f75afcd9a9e425d6ee097a1a1b3190ea98ae78fa4180bbfa8` 的 CLI 1.4.0。构建时校验哈希与版本并生成符号；本次发布使用此前验收容器保存的同哈希二进制。二进制不入 Git；没有此构建时可直接拉取发布镜像，不能用新版 CLI 替换后跳过校验。

实验镜像需要 `SYS_PTRACE` 和 `seccomp=unconfined`。每个会话有独立 CLI/GDB、二进制快照、管道和工作目录；原生池默认上限 2，其他协议仍使用常规池，两池容量分别计算。交付前候选结果错误与 `native_capture_incomplete` 共用一次自动恢复预算：销毁旧进程，按完整客户端历史重新生成。结果不一致、非法原生工具调用及输入清理错误仍直接失败。恢复、失败或取消后销毁相应会话，后续客户端续接使用新进程。实验模式的账号删除会同时检查两个池。

观察器在 TLS 连接关闭时清理两方向的 HTTP 解析器、HPACK 和请求关联；同一对象地址出现新 HTTP/2 前言时重新初始化，前言允许跨 TLS 分片。所有请求已接收完整、但没有 `finish` 的候选，即使 CLI 留有非 null 的 `structured_output`，也进入 `native_finish_missing` 恢复流程。捕获失败记录脱敏事件计数，用于区分无 TLS 观察、请求未关联和响应不完整。

## 验证范围

无 stdin、无常驻 GDB 的替代请求入口尚未接入生产后端，不改变下述支持范围。研究材料仅在本地保留。

已验证 Pi 1.1.0 的内置工具、filesystem MCP、Skills，以及跨 owner 的相同 session ID 隔离和单边取消。native.8 新增图片支持，真实识图验收使用 Gemini Flash low；输入清理只支持已验证的请求形态和固定 CLI。其他模型、远程 MCP OAuth、长时间负载、所有 HTTP 边界与升级兼容尚未完整覆盖。GDB 会影响时序。

v0.0.17 的本地和服务器验收都曾出现模型读完文件后提前结束、未完成后续工具任务，原样复测成功。此问题尚未修复，使用时仍需核实实际文件及检查结果；不能把进程退出码 0 当作任务完成。详见[发布验收记录](releases/v0.0.17.md)。

复验命令、原始实验限制见 [Pi 验收归档](archive/native-chat-pi-20261018.md)与[输入清理归档](archive/native-input-clean-20261018.md)。一般使用不要设置 `AGY_WIRE_INPUT_AUDIT`；它只用于会保存正文的受控审计。
