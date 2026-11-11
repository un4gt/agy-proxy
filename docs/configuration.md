# 环境变量

[返回 README](../README.md) · [部署指南](deployment.md)

应用从进程环境读取配置，不会自行加载 `.env`。Docker Compose 从同目录 `.env` 读取变量，再通过 [docker-compose.yml](../docker-compose.yml) 的 `environment` 传入容器；自定义变量需要同时加入该映射。本地源码运行需在启动前导出环境变量。

可从 [.env.example](../.env.example) 复制模板。修改 `.env` 后执行 `docker compose up -d` 重建需要更新的容器，单独 `restart` 不会加载新的环境变量。

## 鉴权与存储

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AGY_PROXY_PASSWORD` | 空 | 控制台与管理接口密码；未设置时，以 `API_KEYS` 中第一把密钥兜底 |
| `AGY_PROXY_API_KEYS` | 空 | 英文逗号分隔的固定 API Key，也可登录控制台；面板创建的 Key 单独保存在数据目录 |
| `AGY_PROXY_OAUTH_CLIENT_ID` | 空 | 账号信息和额度查询模块刷新过期令牌所用的 OAuth 客户端 ID |
| `AGY_PROXY_OAUTH_CLIENT_SECRET` | 空 | 与上述 ID 配套的客户端密钥，须匹配签发凭据的 OAuth 客户端 |
| `AGY_PROXY_DATA_DIR` | `~/.agy-proxy` | API Key、用量、模型设置、隔离账号凭据、固定身份与账号绑定的数据目录；Docker 内为 `/root/.agy-proxy` |

OAuth 配置用于账号查询模块；CLI 自身的登录与令牌刷新流程独立。未配置这两个值时，账号查询仍可使用未过期凭据。同一 OAuth 客户端授权的多个账号可以共用这套配置，各账号的凭据与 `installation_id` 分别保存。

公开部署需设置控制台密码。客户端建议使用控制台创建、可单独吊销的 API Key；固定 Key 需要修改环境变量并更新容器才能撤销。详细鉴权行为见[部署指南](deployment.md#鉴权与控制台)。

## 网络与执行环境

| 变量 | 默认值 | 用途 |
|---|---|---|
| `PORT` | `8317` | Compose 的宿主机对外端口，映射到容器内 `8317` |
| `AGY_PROXY_HOST` | `127.0.0.1` | 应用监听地址；Docker 镜像设为 `0.0.0.0` |
| `AGY_PROXY_PORT` | `8317` | 应用内部监听端口；修改时须同步容器端口映射与健康检查 |
| `AGY_PROXY_BIN` | `agy` | agy 可执行文件或命令行；bridge 镜像固定 `/opt/agy-bridge/agy`。原生诊断可临时追加 `--v=2` 或 `--vmodule=模块名=2`，详见[原生日志](agy-native-logging.md) |
| `AGY_PROXY_HTTP_PROXY` | 空 | 访问上游的出站代理，如 `http://host.docker.internal:7890`；无协议前缀时补 `http://` |
| `AGY_PROXY_CWD` | 空 | agy 工作目录；建议为空目录，避免加载项目中的规则文件；镜像设为 `/workspace-empty` |

出站代理用于账号查询及 agy 子进程（含登录和更新），并注入子进程的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及小写变体。容器访问宿主机代理时，代理需监听容器可访问的地址；只监听宿主机 `127.0.0.1` 通常无法连接。

代理为所有 agy 子进程设置 `AGY_CLI_DISABLE_AUTO_UPDATE=true`，镜像也内置该变量。CLI 1.4.2 只认精确的 `true`（长度 4），`1` 不会生效。镜像固定 CLI `1.4.2`；后台自更新会在请求之间改变 bridge 二进制与工具契约。升级只能通过重建镜像完成，控制台 `/update/start` 固定返回 `409`。

### Native bridge（固定 1.4.2）

镜像使用由 [app/bridge/patcher.py](../app/bridge/patcher.py) 从固定 SHA `085f43e0…f455ef` 生成的副本 `/opt/agy-bridge/agy`（产物 SHA `636570d0…060649`）。patcher 在 Docker 构建时校验源文件与产物 SHA；任一不匹配都会让构建失败。它保留真实 CLI 登录/bootstrap 与 updater 代码，在 `setUpHTTPServeMux` 返回块注册 `/_bridge`，由代理发送 `GetChatMessageRequest` 并解析原始响应。main 不再包含 stream-json 聊天路径；旧实现保留在 `legay_stream_json_version` 分支。

- 需要 Linux 与可读的 `/proc`（用于发现 CLI 的随机监听端口）。非 Linux 环境会返回 `503`。
- 每个账号保留一个常驻 CLI host；请求之间不保存对话状态，代理每次发送完整消息历史。
- 模型 slug 到内部枚举的映射固定于 1.4.2 目录（如 `gemini-3.8-flash-medium → 1319`）；升级 CLI 必须重新提取目录并更新 patcher，不能只替换二进制。
- 工具调用是原生 function calling。Gemini 的 `thinkingSignature` 由代理按 tool call ID 缓存，并持久化到数据目录的 `bridge-signatures.json`（7 天/最多 4096 条，0600）；容器重启后仍可续轮。升级 CLI 或清空数据目录会使旧签名失效。
- 图片输入已接入 bridge：代理把已验证的 PNG/JPEG/WebP/GIF 以 `media.inlineData` 发送；数量、字节和像素限制与旧路径相同（最多 8 张、20 MiB、32 MP）。图片 URL 仍由代理先下载并校验。
- bridge 处理器使用 `(*http.Request).Context()` 调用上游；客户端断开时取消信号会传入 `GetChatMessage`。2026-11-11 实测 3 秒断连后 `raw.Err`/`Close` 在约 3 秒内命中，后续请求正常。
- `/update/start` 固定返回 `409`，避免 `agy update` 覆盖固定 SHA 副本；升级通过重建镜像完成。
- 只支持 Linux 与可读的 `/proc`；源码运行必须指向已打补丁的 1.4.2 二进制，否则启动 bridge host 失败。

升级固定 CLI 的步骤：

1. 更新 Dockerfile 的 `AGY_VERSION`/`AGY_URL`/`AGY_SHA512`，并确认源文件 SHA。
2. 用新源运行 `python app/bridge/patcher.py --source <new> --dest <out> --allow-new`，记录产物 SHA；更新 `app/bridge/__init__.py`、patcher 的回退常量以及 `tests/test_bridge_protocol.py`。
3. 重新提取该构建的 `Model` 目录并更新 `app/bridge/models.py`；旧枚举在新版本可能变化。
4. 跑 `tests/test_bridge_protocol.py` 和真实 bridge 冒烟（文本、流式、工具续轮、图片、取消）。
5. 重建镜像；`/update/start` 在 bridge 下始终返回 409，升级只通过镜像完成。

## 超时、缓存与进程

时间单位均为秒。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AGY_PROXY_MAX_SESSIONS` | `8` | 常驻 bridge host 数上限，最低 1；满时回收空闲 host，全忙返回 429 |
| `AGY_PROXY_TURN_TIMEOUT` | `600` | 单轮生成总超时，最低 5；不包含所有请求前置操作 |
| `AGY_PROXY_STREAM_HEARTBEAT` | `10` | 流式等待期间发送 SSE 心跳的间隔，最低 0.1 |
| `AGY_PROXY_SESSION_TTL` | `1800` | 空闲 bridge host 的回收时间，最低 30 |
| `AGY_PROXY_MODELS_TTL` | `300` | `agy models` 列表缓存时间，最低 10 |

SSE 心跳用于维持 HTTP 连接，不表示模型生成了内容。`SESSION_TTL` 只回收 host，不清除账号身份或持久化绑定。`TURN_TIMEOUT` 是单轮上游请求的总预算；客户端断开时通过 `Request.Context` 取消上游。bridge 不包含 stream-json 的恢复重放或 NDJSON 解析。

## 模型与 agy 参数

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AGY_PROXY_DEFAULT_MODEL` | 空 | 默认模型映射目标；各 API 的未知模型校验见[接入文档](usage.md) |
| `AGY_PROXY_MODEL_MAP` | 空 | JSON 模型别名映射，如 `{"my-model":"gemini-3.8-flash-high"}` |
| `AGY_PROXY_EFFORT` | 空 | 请求未指定时使用的默认思考级别 |

模型别名与默认映射仍受模型禁用设置约束。代理不设置模型上下文窗口或提示字符上限；消息历史和工具定义完整交给 bridge，由 AGY 与上游模型处理上下文容量。HTTP 请求体的 32 MiB 传输限制及图片数量、字节和像素限制独立保留。客户端的 `contextWindow` 用于客户端自身的上下文管理，不由代理修改；bridge 的上游目录支持接近 1M 的输入，实测见[客户端接入](usage.md#模型上下文与输出上限)。

## 链路计时与日志

推理请求完成时默认输出脱敏的 `request_latency` JSON 日志，用 `X-Agy-Request-Id` 关联模型查询、会话锁等待、进程启动、各次生成、校验、恢复和 HTTP 发送。每请求一条、阶段数量有上限；不记录提示词、工具参数或模型正文。Compose 日志轮转默认每文件 20 MB、保留 5 份，无额外环境变量。

详见[链路耗时分析](latency-observability.md)，包含实际测试时间线、Perfetto 导出命令、字段定义及 CPU 采样与墙钟等待的区别。
