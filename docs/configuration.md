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
| `AGY_PROXY_BIN` | `agy` | agy 可执行文件或命令行，如 `python fake.py`；原生诊断可临时追加 `--v=2` 或 `--vmodule=模块名=2`，详见[原生日志](agy-native-logging.md)。bridge 镜像设为 `/opt/agy-bridge/agy` |
| `AGY_PROXY_BRIDGE` | `false` | 使用固定 SHA 1.4.2 原生 bridge；镜像默认 `true`。开启后不再使用 stream-json/agent 路径，详见下方“原生 bridge” |
| `AGY_PROXY_HTTP_PROXY` | 空 | 访问上游的出站代理，如 `http://host.docker.internal:7890`；无协议前缀时补 `http://` |
| `AGY_PROXY_CWD` | 空 | agy 工作目录；建议为空目录，避免加载项目中的规则文件；镜像设为 `/workspace-empty` |

出站代理用于账号查询及 agy 子进程（含登录和更新），并注入子进程的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及小写变体。容器访问宿主机代理时，代理需监听容器可访问的地址；只监听宿主机 `127.0.0.1` 通常无法连接。

代理为所有 agy 子进程设置 `AGY_CLI_DISABLE_AUTO_UPDATE=true`，镜像也内置该变量。CLI 1.4.2 只认精确的 `true`（长度 4），`1` 不会生效。镜像固定 CLI `1.4.2`；后台自更新会在请求之间改变协议与工具契约，增加难以复现的差异（`1.5.4` 把图片读取从 `view_file`/`AbsolutePath` 改为 `read_file`/`Path`，代理已同时接受两种别名）。手动控制台的“更新”操作仍执行显式 `agy update`，不受该变量影响；升级后应先跑图片、工具调用和流式回归。

### 原生 bridge（1.4.2）

镜像默认 `AGY_PROXY_BRIDGE=true`，使用由 [app/bridge/patcher.py](../app/bridge/patcher.py) 从固定 SHA `085f43e0…f455ef` 生成的副本 `/opt/agy-bridge/agy`（产物 SHA `0a32d852…056259`）。patcher 在 Docker 构建时校验源文件与产物 SHA；任一不匹配都会让构建失败。它保留真实 CLI 登录/bootstrap 与 updater 代码，在 `setUpHTTPServeMux` 返回块注册 `/_bridge`，由代理发送 `GetChatMessageRequest` 并解析原始响应。

- 需要 Linux 与可读的 `/proc`（用于发现 CLI 的随机监听端口）。非 Linux 环境会返回 `503`。
- 每个账号保留一个常驻 CLI host；请求之间不保存对话状态，代理每次发送完整消息历史。
- 模型 slug 到内部枚举的映射固定于 1.4.2 目录（如 `gemini-3.8-flash-medium → 1319`）；升级 CLI 必须重新提取目录并更新 patcher，不能只替换二进制。
- 工具调用是原生 function calling。Gemini 的 `thinkingSignature` 由代理按 tool call ID 缓存，并持久化到数据目录的 `bridge-signatures.json`（7 天/最多 4096 条，0600）；容器重启后仍可续轮。升级 CLI 或清空数据目录会使旧签名失效。
- 图片输入已接入 bridge：代理把已验证的 PNG/JPEG/WebP/GIF 以 `media.inlineData` 发送；数量、字节和像素限制与旧路径相同（最多 8 张、20 MiB、32 MP）。图片 URL 仍由代理先下载并校验。
- bridge 处理器使用 `(*http.Request).Context()` 调用上游；客户端断开时取消信号会传入 `GetChatMessage`。2026-11-11 实测 3 秒断连后 `raw.Err`/`Close` 在约 3 秒内命中，后续请求正常。
- bridge 模式下 `/update/start` 返回 `409`，避免 `agy update` 覆盖固定 SHA 副本；升级通过重建镜像完成。
- 与 `AGY_PROXY_NATIVE_CHAT=true` 同时设置时 bridge 优先。
- `AGY_PROXY_BRIDGE=false` 仅保留为迁移回退；bridge 稳定后计划移除 stream-json 代码和对应旧测试。
- 升级现有 Compose 时，确认没有覆盖镜像内置的 `AGY_PROXY_BIN=/opt/agy-bridge/agy` 或把 `AGY_PROXY_BRIDGE` 设为 `false`；否则会回到未打补丁的 CLI/stream-json 路径。

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
| `AGY_PROXY_MAX_SESSIONS` | `8` | agy 进程数上限，最低 1；满时回收空闲进程，全忙返回 429 |
| `AGY_PROXY_TURN_TIMEOUT` | `600` | 单轮生成总超时，最低 5；不包含所有请求前置操作 |
| `AGY_PROXY_RECOVERY_ATTEMPTS` | `1` | 请求共享恢复预算：参数修正、可恢复传输故障、配额换号总共最多一次；`0` 关闭，其他整数限制到 0–1 |
| `AGY_PROXY_IDLE_TIMEOUT` | `0` | CLI stdout 静默超时；0 禁用，正数表示允许无输出的时长 |
| `AGY_PROXY_STREAM_HEARTBEAT` | `10` | 流式等待期间发送 SSE 心跳的间隔，最低 0.1 |
| `AGY_PROXY_SESSION_TTL` | `1800` | 空闲会话及预热进程的回收时间，最低 30 |
| `AGY_PROXY_MODELS_TTL` | `300` | `agy models` 列表缓存时间，最低 10 |
| `AGY_PROXY_PREWARM_MODELS` | 空 | 英文逗号分隔的模型 slug；提前启动空闲进程以减少冷启动，消耗进程名额和内存 |

模型思考时可能没有 stdout 输出，通常保留 `IDLE_TIMEOUT=0`；单轮总超时仍由 `TURN_TIMEOUT` 控制。SSE 心跳用于维持 HTTP 连接，不表示模型生成了内容。`SESSION_TTL` 只回收进程，不清除账号身份或持久化绑定，也不触发换号。

原始尝试与恢复共用 `TURN_TIMEOUT`，重建进程不会重置截止时间。所有四种协议的正文均等待完整校验；开始事件和心跳之后仍可恢复，正文交付之后不重新生成。参数修正保留账号、模型和 effort；只有明确的配额耗尽可以换号。取消、超时、鉴权失败、NDJSON 协议损坏及原生工具违规不恢复。

## 模型与 agy 参数

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AGY_PROXY_DEFAULT_MODEL` | 空 | 默认模型映射目标；各 API 的未知模型校验见[接入文档](usage.md) |
| `AGY_PROXY_MODEL_MAP` | 空 | JSON 模型别名映射，如 `{"my-model":"gemini-3.8-flash-high"}` |
| `AGY_PROXY_EFFORT` | 空 | 请求未指定时使用的默认思考级别 |
| `AGY_PROXY_AGENT` | 空 | 内部旧执行入口的默认 `--agent`；四种客户端推理 API 均忽略此项 |
| `AGY_PROXY_SKIP_PERMISSIONS` | `false` | 内部旧执行入口的权限参数；四种客户端推理 API 均不传入跳过权限标记 |
| `AGY_PROXY_SANDBOX` | `false` | 向 agy 传入 `--sandbox` |

布尔值接受 `1`、`true`、`yes`、`on`，不区分大小写。模型别名与默认映射仍受模型禁用设置约束。客户端工具模式使用受限 Agent，不采用上述自定义 Agent 或跳过权限配置。

代理不设置模型上下文窗口或提示字符上限；消息历史和工具定义完整交给 AGY，由 AGY 与上游模型处理上下文容量。HTTP 请求体的 32 MiB 传输限制及图片数量、字节和像素限制独立保留。客户端的 `contextWindow` 用于客户端自身的上下文管理，不由代理修改；上游目录值与当前 CLI 约 55K 的实际输入上限见[客户端接入](usage.md#模型上下文与输出上限)。

## 原生实验后端

以下变量需使用 [原生实验镜像与独立 Compose](native-backend.md)。默认正式 Compose 不启用原生后端。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AGY_PROXY_NATIVE_CHAT` | `false` | 仅 Chat Completions 选择原生后端 |
| `AGY_PROXY_NATIVE_INPUT_CLEAN` | `false` | 原生后端清理已验证的 CLI 输入包装 |
| `AGY_PROXY_NATIVE_BIN` | 空，沿用 `BIN` | 原生后端独立二进制；实验镜像固定 `/opt/agy-native/agy` |
| `AGY_PROXY_NATIVE_SYMBOLS` | `/evidence/symbols.json` | 实验镜像固定 `/opt/agy-native/symbols.json` |
| `NATIVE_BIND` / `NATIVE_PORT` | `127.0.0.1` / `18317` | 独立实验 Compose 的宿主机监听地址 |
| `AGY_PROXY_NATIVE_MAX_SESSIONS` | `2` | 独立实验 Compose 映射到该服务的 `MAX_SESSIONS` |

## 链路计时与日志

推理请求完成时默认输出脱敏的 `request_latency` JSON 日志，用 `X-Agy-Request-Id` 关联模型查询、会话锁等待、进程启动、各次生成、校验、恢复和 HTTP 发送。每请求一条、阶段数量有上限；不记录提示词、工具参数或模型正文。Compose 日志轮转默认每文件 20 MB、保留 5 份，无额外环境变量。

详见[链路耗时分析](latency-observability.md)，包含实际测试时间线、Perfetto 导出命令、字段定义及 CPU 采样与墙钟等待的区别。
