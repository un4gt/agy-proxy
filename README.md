# agy-proxy

将 [Antigravity CLI（agy）](https://antigravity.google/docs/cli/headless/) 包装为兼容 OpenAI、Gemini 和 Anthropic 的 API 服务，可接入 Pi、Codex、Claude Code、OpenCode 等客户端。

- 支持 Chat Completions、Responses、Messages、Gemini API，以及 SSE 流式响应。
- 支持客户端工具调用、用户图片输入和 JSON Schema 输出，具体组合与限制见[接入文档](docs/usage.md)。
- 内置控制台：管理账号与 API Key、查看用量和额度、启停模型、查看 Core/Full 运行版本。
- 多账号独立保存凭据和安装标识；同一 API Key 固定使用同一账号，仅额度耗尽时切换。

> [!WARNING]
> **本项目与 agy CLI 版本强绑定，不要随意升级 agy CLI。**
> 镜像固定使用 agy CLI **1.4.2**（固定下载地址 + SHA512 校验）；聊天后端是对该版本 Linux 二进制打补丁后注册的 `/_bridge`，补丁依赖这一构建的固定文件 SHA、函数地址和二进制偏移。换一个 CLI 版本，补丁会在构建时直接拒绝，或产出无法工作的二进制。
>
> - 不要执行 `agy update`；不要在容器内替换 `/opt/agy-bridge/agy`；不要单独修改 Dockerfile 的 `AGY_VERSION`、`AGY_URL`、`AGY_SHA512` 后直接发布。
> - 任何 CLI 升级都必须重新逆向并更新 `core/agy_proxy_core/bridge/patcher.py` 与 `emitter.py`，重新提取模型枚举，并完成 bridge、登录、额度、模型目录和客户端协议回归，再重建镜像。步骤见[部署指南](docs/deployment.md#cli-版本与更新)。

## 项目结构

- `core/agy_proxy_core/`：账号登录/路由/隔离、机器标识、store、浏览器控制台、bridge、Gemini 协议服务。
- `adapters/agy_proxy_adapters/`：OpenAI Chat Completions、Responses、Anthropic Messages 与 core 的协议转换；只依赖 core，不接触账号或 bridge。
- `src/`：React 控制台源码；构建产物在 `core/agy_proxy_core/static/`。
- `tests/`、`core/tests/`、`adapters/tests/`：单元测试、core/full 工厂测试和边界检查。

`core-v*` 镜像只安装 core；`full-v*` 镜像安装 core + adapters。两个镜像共用数据卷和 `/health`，默认 Compose 仍是 full。

## Docker 快速开始

镜像已内置固定版本 agy CLI `1.4.2`，当前版本为 `tumuer/agy-proxy:full-v0.0.20`（core + OpenAI/Responses/Anthropic 适配器）。只要 Gemini 协议和账号/面板时，使用 `tumuer/agy-proxy:core-v0.0.20` 与 `docker-compose.core.yml`。`latest` 指向 full。需要 Docker Compose：

```bash
git clone https://github.com/un4gt/agy-proxy.git
cd agy-proxy
cp .env.example .env
# 编辑 .env，设置 AGY_PROXY_PASSWORD 作为控制台登录密码
docker compose up -d
```

1. 打开 `http://服务器IP:8317`，使用控制台密码登录。
2. 进入「账号详情」，添加账号并在该账号卡片中完成 Google 授权。
3. 在控制台创建 API Key，填入客户端后选择可用模型。

compose 默认启动 full 镜像。只用 Gemini 协议时：

```bash
docker compose -f docker-compose.core.yml up -d
```

两个 compose 文件使用同一组 `agy-gemini` / `agy-data` 卷，切换镜像不会丢账号或用量。

配置模板见 [.env.example](.env.example)。`agy-gemini` 和 `agy-data` 两个卷保存登录凭据、账号身份、绑定、密钥与用量，升级时保留并备份整个数据目录。登录流程、升级及恢复方法见[部署指南](docs/deployment.md)。

## 客户端接入

将下表中的地址替换为你的服务器地址，API Key 使用控制台创建的密钥：

| 协议 | Base URL | 主要接口 |
|---|---|---|
| OpenAI Chat Completions | `http://服务器IP:8317/v1` | `POST /v1/chat/completions` |
| OpenAI Responses（Codex） | `http://服务器IP:8317/v1` | `POST /v1/responses` |
| Anthropic Messages（Claude Code） | `http://服务器IP:8317` | `POST /v1/messages` |
| Gemini | `http://服务器IP:8317` | `POST /v1beta/models/{model}:generateContent` |

用 Bash 查询可用模型，再将示例中的模型名替换为列表中的 ID：

```bash
export AGY_API_KEY="控制台创建的API密钥"

curl http://127.0.0.1:8317/v1/models \
  -H "Authorization: Bearer $AGY_API_KEY"

curl http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer $AGY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.8-flash-high","messages":[{"role":"user","content":"你好"}]}'
```

模型思考级别、Pi / Codex / Claude Code 配置、MCP、图片和工具调用示例见[客户端接入](docs/usage.md)。

## 多账号与会话

- 每个账号拥有独立目录和固定 `installation_id`；重启、重新授权及进程回收保留已有标识。
- 同一客户端 API Key 的账号绑定持久化保存。只有明确额度耗尽才切换，普通 429、超时、网络或认证错误均不会换号；切到 B 后，A 恢复也不会自动切回。
- 每个账号维护一个常驻 bridge host；host 只负责认证和上游客户端，请求之间不保存对话状态。代理按客户端会话标识或完整历史关联连续请求，并在每轮发送完整消息历史。

账号数据结构、耗尽判定和验证范围见[多账号隔离说明](docs/account-isolation.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [部署与运维](docs/deployment.md) | Docker、授权登录、控制台、备份、升级与排查 |
| [环境变量](docs/configuration.md) | 密码、API Key、OAuth、代理、超时及进程上限 |
| [客户端接入](docs/usage.md) | API 示例、模型选择、工具、图片、会话与兼容限制 |
| [多账号隔离](docs/account-isolation.md) | 固定安装标识、账号绑定与额度耗尽后切换 |
| [本地开发](docs/development.md) | 源码启动、前端构建、测试与性能分析 |
| [历史归档](docs/archive/README.md) | 阶段性研究与客户端验收记录 |

源码运行需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和已登录的 agy CLI。注意：源码运行同样只支持固定 `1.4.2`，必须用 `AGY_PROXY_BIN` 指向 [patcher](core/agy_proxy_core/bridge/patcher.py) 生成的副本，不能直接使用系统里任意版本的 `agy`；升级 CLI 前先阅读[部署指南的版本说明](docs/deployment.md#cli-版本与更新)。

```bash
uv sync
uv run main.py
# 运行协议测试，无需真实账号
uv run pytest
```
