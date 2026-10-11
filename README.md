# agy-proxy

将 [Antigravity CLI（agy）](https://antigravity.google/docs/cli/headless/) 包装为兼容 OpenAI、Gemini 和 Anthropic 的 API 服务，可接入 Pi、Codex、Claude Code、OpenCode 等客户端。

- 支持 Chat Completions、Responses、Messages、Gemini API，以及 SSE 流式响应。
- 支持客户端工具调用、用户图片输入和 JSON Schema 输出，具体组合与限制见[接入文档](docs/usage.md)。
- 内置控制台：管理账号与 API Key、查看用量和额度、启停模型、更新 agy CLI。
- 多账号独立保存凭据和安装标识；同一 API Key 固定使用同一账号，仅额度耗尽时切换。

## Docker 快速开始

镜像已内置 agy CLI，当前版本为 `tumuer/agy-proxy:v0.0.14`。需要 Docker Compose：

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
- 连续对话按客户端会话标识或完整历史匹配，复用对应的独立 agy 进程。不同会话保持隔离，账号绑定独立于会话的创建与回收。

账号数据结构、耗尽判定和验证范围见[多账号隔离说明](docs/account-isolation.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [部署与运维](docs/deployment.md) | Docker、授权登录、控制台、备份、升级与排查 |
| [环境变量](docs/configuration.md) | 密码、API Key、OAuth、代理、超时及进程上限 |
| [客户端接入](docs/usage.md) | API 示例、模型选择、工具、图片、会话与兼容限制 |
| [多账号隔离](docs/account-isolation.md) | 固定安装标识、账号绑定与额度耗尽后切换 |
| [本地开发](docs/development.md) | 源码启动、前端构建、测试与性能分析 |

源码运行需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和已登录的 agy CLI：

```bash
uv sync
uv run main.py
# 运行协议测试，无需真实账号
uv run pytest
```
