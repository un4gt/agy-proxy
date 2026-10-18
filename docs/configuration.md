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
| `AGY_PROXY_BIN` | `agy` | agy 可执行文件或命令行，如 `python fake.py`；原生诊断可临时追加 `--v=2` 或 `--vmodule=模块名=2`，详见[原生日志](agy-native-logging.md) |
| `AGY_PROXY_HTTP_PROXY` | 空 | 访问上游的出站代理，如 `http://host.docker.internal:7890`；无协议前缀时补 `http://` |
| `AGY_PROXY_CWD` | 空 | agy 工作目录；建议为空目录，避免加载项目中的规则文件；镜像设为 `/workspace-empty` |

出站代理用于账号查询及 agy 子进程（含登录和更新），并注入子进程的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及小写变体。容器访问宿主机代理时，代理需监听容器可访问的地址；只监听宿主机 `127.0.0.1` 通常无法连接。

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

代理不设置模型上下文窗口或提示字符上限；消息历史和工具定义完整交给 AGY，由 AGY 与上游模型处理上下文容量。HTTP 请求体的 32 MiB 传输限制及图片数量、字节和像素限制独立保留。客户端的 `contextWindow` 用于客户端自身的上下文管理，不由代理修改。

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
