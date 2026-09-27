# agy-proxy

把 [Antigravity CLI（`agy`）](https://antigravity.google/docs/cli/headless/) 的 **stream-json 双向模式**包装成标准 API 服务的反向代理：

- **内置控制台**：打开首页即可看 agy 当前可用模型、token 用量（取自 agy 自己的 usage 计数），并**创建/吊销上游 API Key**
- **Gemini API** 兼容：`POST /v1beta/models/{model}:generateContent`、`:streamGenerateContent`（SSE）、`GET /v1beta/models`、`:countTokens`
- **OpenAI API** 兼容：`POST /v1/chat/completions`（支持流式与 `stream_options.include_usage`）、`GET /v1/models`
- **容器内 agy 自更新**：`/update` 页面一键升级容器内的 agy CLI，无需重建镜像

```bash
docker pull tumuer/agy-proxy:v0.0.8
```

任何支持自定义 base URL 的客户端（Cherry Studio、OpenWebUI、LobeChat、Continue、openai/gemini SDK 等）都能直接接入。

## 工作原理

```
客户端 (OpenAI / Gemini 格式)
   │  HTTP + SSE
   ▼
agy-proxy (FastAPI)
   │  stdin  ← NDJSON: {"event":"user","message":{"content":"..."}}
   │  stdout → NDJSON: init / step_update(text_delta) / result
   ▼
agy --input-format stream-json --output-format stream-json  (常驻进程)
```

- 每个会话对应一个常驻 `agy` 进程，prompt 经 **stdin** 传入（无命令行长度限制）
- `step_update`（`step_type=agent_response`）的 `text_delta` 增量转发为 SSE；`DONE` 事件携带该步完整文本，代理只补发未发送部分
- 每轮以恰好一个 `result` 事件结束，`usage`（input/output/thinking tokens）映射为对应格式的 usage 字段

### 多轮会话复用

客户端每次请求都携带全量历史。代理按 `(模型, effort, system, 历史消息)` 哈希匹配**已有 agy 会话**：

- 命中空闲会话 → 只发送最后一条用户消息（利用 agy 进程内原生上下文，省 token）
- 未命中 / 会话忙（分支、重新生成）→ 新起进程，把完整对话拍平为一条 seed prompt
- LRU + 空闲 TTL 淘汰，上限 `AGY_PROXY_MAX_SESSIONS`

## 前置条件

1. 已安装 Antigravity CLI：`agy --version`（[安装文档](https://antigravity.google/docs/cli/)）
2. **先交互式登录一次**：终端运行 `agy`，完成 OAuth。headless 模式复用本机凭据
   （未登录时所有请求会返回 502 及 agy 的认证错误信息）

## 启动

```bash
uv sync
uv run main.py            # 等价于 uvicorn app:app --host 127.0.0.1 --port 8317
```

## 配置（环境变量，前缀 `AGY_PROXY_`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BIN` | `agy` | agy 可执行文件（可含完整命令行，如 `python fake.py`） |
| `HOST` / `PORT` | `127.0.0.1` / `8317` | 监听地址 |
| `PASSWORD` | 空 | **控制台密码**，保护 `/`、`/login`、`/update`、`/dashboard`、`/api/*`。为空则控制台无鉴权（仅适合本机） |
| `API_KEYS` | 空 | 逗号分隔的**固定** API 密钥，供客户端调 `/v1/*`；也兼作控制台登录凭证。常规做法是留空、改在控制台里创建 |
| `DATA_DIR` | `~/.agy-proxy` | API Key 与用量记录（`data.json`）所在目录 |
| `DEFAULT_MODEL` | 空 | 未知模型兜底映射到的 slug |
| `MODEL_MAP` | 空 | JSON，如 `{"gemini-3-pro-preview":"gemini-3-pro-high"}` |
| `MAX_SESSIONS` | `8` | 最大并发 agy 进程数（LRU 淘汰） |
| `SESSION_TTL` | `1800` | 空闲会话回收秒数 |
| `TURN_TIMEOUT` | `600` | 单轮总超时秒数 |
| `IDLE_TIMEOUT` | `120` | 单轮无输出超时秒数 |
| `MODELS_TTL` | `300` | `agy models` 结果缓存秒数 |
| `EFFORT` / `AGENT` | 空 | 进程级默认 `--effort` / `--agent` |
| `SKIP_PERMISSIONS` | `false` | 传 `--dangerously-skip-permissions`（允许 agy 执行工具，谨慎） |
| `SANDBOX` | `false` | 传 `--sandbox` |
| `HTTP_PROXY` | 空 | agy 子进程的出站代理（如 `127.0.0.1:7890`，自动补 `http://`），注入为 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 及小写变体。**网络需代理才能访问 Google 时必设**，否则 agy 会报 "Please sign in" 这类误导性错误 |
| `CWD` | 空 | agy 子进程工作目录。建议指向一个空目录，避免代理运行目录下的 `AGENTS.md`/`GEMINI.md` 等规则文件被注入上下文 |
| `PREWARM_MODELS` | 空 | 逗号分隔的模型 slug（如 `gemini-3.1-pro-high`）。为每个模型预热一个空闲会话，首个请求可省去约 6 秒的 agy 进程冷启动（实测 41.4s → 23.7s，剩余为模型思考时间） |

模型解析顺序：`MODEL_MAP` 精确匹配 → `agy models` 列表精确匹配 → 子串模糊匹配 → `DEFAULT_MODEL` → 原样透传（由 agy 报错）。
`effort` 来自 Gemini 的 `generationConfig.thinkingConfig.thinkingLevel` 或 OpenAI 的 `reasoning_effort`（low/medium/high）。

## 使用示例

### Gemini 格式

```bash
# 非流式
curl -X POST "http://127.0.0.1:8317/v1beta/models/gemini-3-pro-high:generateContent" \
  -H "content-type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"用一句话介绍你自己"}]}]}'

# 流式 (SSE)
curl -N -X POST "http://127.0.0.1:8317/v1beta/models/gemini-3-pro-high:streamGenerateContent?alt=sse" \
  -H "content-type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"讲个笑话"}]}]}'
```

Gemini SDK 接入：`client = genai.Client(api_key="任意", http_options={"base_url": "http://127.0.0.1:8317"})`

### OpenAI 格式

```bash
curl -X POST http://127.0.0.1:8317/v1/chat/completions \
  -H "content-type: application/json" \
  -d '{"model":"gemini-3-pro-high","messages":[{"role":"user","content":"你好"}]}'
```

openai SDK 接入：`OpenAI(base_url="http://127.0.0.1:8317/v1", api_key="任意或配置的密钥")`

兼容路径变体：`:generateContent` 与 `/generateContent`、`/v1beta/` 与 `/v1/`、带或不带 `models/` 前缀均可。

## 鉴权（公网部署必读）

两把**互不通用**的凭据，职责分开：

| 凭据 | 用途 | 来源 |
|---|---|---|
| **控制台密码** `AGY_PROXY_PASSWORD` | 登录 `/signin` → 使用 `/`（控制台）、`/login`、`/update`、`/dashboard`、`/api/*` | 环境变量。**控制台自身的凭证，不能给客户端当 API Key 用** |
| **上游 API Key** | 客户端调用 `/v1/*`、`/v1beta/*` | 控制台「API Keys」里创建（可随时吊销），或 `AGY_PROXY_API_KEYS` 里配置的固定密钥 |

- **网页登录**：未携带有效凭证访问面板页面会跳转到 **`/signin`**，填控制台密码即可；通过后服务端种下 7 天有效的 HttpOnly Cookie。退出登录访问 `/signout`
- **API 调用**：接受 `Authorization: Bearer <key>`、`Basic`（`curl -u :key`）、`x-api-key`、`x-goog-api-key`、`?key=`、以及浏览器 Cookie 任一形式
- 兼容：未设置 `AGY_PROXY_PASSWORD` 时，`AGY_PROXY_API_KEYS` 里的密钥仍可登录控制台（升级旧部署不用改 `.env`；启动日志会打 WARNING 提示补设 PASSWORD）
- `/health`：仅返回状态/版本/是否已设置鉴权/是否已登录，不泄露部署细节；`/docs`、`/openapi.json` 已关闭
- 未设置任何凭据时服务完全开放（`/signin` 会提示"未设置鉴权"），启动日志会打 WARNING——仅适合本机使用

## 控制台

打开 `http://<IP>:<PORT>/`，四个区块：

1. **用量** — 累计请求数、输入/输出/思考/缓存命中 token（**计数直接取自 agy 每次 result 事件的 `usage` 字段**，代理不做估算）
2. **可用模型** — `agy models` 的实时列表（含人类可读标签），并叠加每个模型的请求数与 token 消耗、相对用量条
3. **API Keys** — 创建（明文只在创建时展示一次）、改名、删除。删除后客户端立即收到 401；每把 key 单独统计请求数与 token
4. **最近请求** — 时间、模型、使用哪把 key、状态（成功/错误码/中断）、耗时、token

数据写在 `AGY_PROXY_DATA_DIR`（默认 `~/.agy-proxy/data.json`），改动静默去抖落盘、启动时读回；Docker 部署已挂 volume `agy-data`。
页面每 5 秒自动刷新。管理接口：`GET /dashboard`、`GET /api/models`、`POST|PATCH|DELETE /api/keys[/{id}]`（均需控制台凭证）。
样式来自一份编译好的 `app/static/app.css`，四个页面共用（见「前端样式」）。

## 已知限制

- **内置 agent 开销无法去除**：每次请求约 13.5k token 上下文（agy 内置 agent 系统提示 + 57 个工具定义），实测换 cwd、`--disable-slash-commands`、`GEMINI_SYSTEM_MD`、`--agent` 均无法剥离。好消息是其中约 8k 是 `cache_read_tokens`（缓存命中，计费约为常规输入的 1/4），且多轮会话复用会让缓存占比更高。真要"干净"的模型需走官方 Gemini API
- **非文本内容**（图片 inlineData、function calling）不透传，记日志后以占位文本替代；`tools`/`temperature` 等参数忽略
- agy 是 agent，回复可能包含其工具使用过程（受其自身权限策略约束）；纯问答一般不触发
- 每个新会话首轮有进程冷启动开销（秒级）
- 需遵守 Antigravity 服务条款与账号额度，本代理不做额外限流（按 key 只做统计，不做配额；可用 `MAX_SESSIONS` 约束并发）
- 控制台用量只保留**最近 500 条**请求明细（更早的只进总量与按模型聚合），且首次启动前的历史不可追溯

## 测试

```bash
uv run pytest    # 使用 tests/fake_agy.py 模拟 agy 协议，无需真实登录
```

## 前端（React + shadcn/ui）

控制台、登录、账号连接和 CLI 更新四个页面使用 **React 19 + TypeScript + Vite + Tailwind CSS v4**。通过官方 shadcn CLI 安装的 New York / Radix 组件源码位于 `src/components/ui/`，包括 Button、Card、Table、Tabs、Field、Dialog、AlertDialog 和 Sonner；配置见 `components.json`。

```bash
npm ci
npm run build          # 类型检查并编译到 app/static/
npm run dev            # Vite 开发服务器（数据接口转发到本机 8317）
```

样式及主题统一在 `src/index.css`，应用入口为 `src/main.tsx`。`npm run build:css` 兼容旧命令，现在会重新构建整个前端。登录 POST 请求在开发服务器中通过代理转发；生产环境全部由 FastAPI 同源提供。

`app/static/index.html`、`app/static/app.css`、`app/static/assets/` 是需要一起提交的编译产物。生产运行和 Docker 镜像无需 Node、CDN 或外部字体。修改前端后先运行 `npm run build`，再刷新页面；修改 Python 路由后需要重启服务。

可运行浏览器回归检查（使用独立临时数据与模拟 agy，不操作真实账号）：

```bash
uv run --with playwright python tests/browser_smoke.py
```

该检查默认使用本机 Edge；其他环境可设置 `UI_BROWSER_CHANNEL=chromium` 并先安装 Playwright Chromium。

## Docker 部署（含远程登录桥）

镜像内置 agy CLI（官方 install.sh 安装），凭据持久化在 volume `agy-gemini`（`/root/.gemini`），一次登录长期有效。镜像已发布：`docker.io/tumuer/agy-proxy`。

### 一条命令启动（docker compose + .env）

服务器上只需两个文件（都在本仓库里）：

```bash
mkdir agy-proxy && cd agy-proxy
# 放入 docker-compose.yml，然后：
cp .env.example .env   # 或从仓库拷贝 .env.example
vim .env               # 至少填 AGY_PROXY_PASSWORD（openssl rand -hex 32 生成）

docker compose up -d
docker compose logs -f   # 确认启动正常
```

浏览器打开 `http://<IP>:<PORT>/` → 跳转到 `/signin` → 填控制台密码 → 进入控制台。agy 未登录时先去 `http://<IP>:<PORT>/login` 完成授权。

升级：改 `.env` 后 `docker compose up -d`；换镜像版本改 `docker-compose.yml` 里的 tag（如 `tumuer/agy-proxy:v0.0.8`）后 `docker compose pull && docker compose up -d`。

### 手动 docker run（等价）

```bash
docker run -d --name agy-proxy -p 8317:8317 \
  -v agy-gemini:/root/.gemini \
  -v agy-data:/root/.agy-proxy \
  -e AGY_PROXY_HTTP_PROXY=http://host.docker.internal:7890 \
  -e AGY_PROXY_PASSWORD=你的控制台密码 \
  --add-host=host.docker.internal:host-gateway \
  tumuer/agy-proxy:v0.0.8
```

### 容器内 agy 的 OAuth 登录（浏览器在你自己的电脑上完成）

容器没有浏览器，代理内置了登录桥：用 PTY 驱动 agy 的登录界面，把 Google 授权链接透出到网页上。

1. 打开 `http://<部署机IP>:8317/login`，点 **开始登录**
2. 页面出现 **打开 Google 授权页** 按钮 → 在你自己的电脑浏览器里打开并完成授权
3. 授权后浏览器会跳转到 `https://antigravity.google/oauth-callback?...`——从该页面（或地址栏的 `code=` 参数）复制授权码
4. 粘回 `/login` 页面的输入框并提交；状态变为 **已登录 ✓** 即成功（凭据写入 volume）

说明：授权码无效会自动返回重试；整次登录有 15 分钟超时；登录进程只在登录期间存在。`/login` 与控制台同权限（需登录后才能访问）。

### 容器内更新 agy CLI（无需重建镜像）

控制台页头的 **agy vX.Y.Z** 徽章即当前容器内 agy 版本，点进去（或直接访问 `/update`）：

1. 页面显示当前版本，点 **⟳ 检查并更新**
2. 代理执行官方 `agy update`（检查 → 下载 → 校验 → 安装），进度实时显示
3. 完成后新会话自动使用新版本；已是最新会直接提示，无需重启容器

说明：更新前会先结束**空闲** agy 会话以释放旧版本二进制（进行中的请求不受影响，用完旧版后自然重建）；容器重建（`docker compose up -d` 换镜像）后 agy 会回到镜像内置版本，属预期。`AGY_PROXY_HTTP_PROXY` 同样作用于更新子进程。API：`GET /update/status`、`POST /update/start`（与控制台相同的鉴权约定）。

## 延迟分析（profiling）

每个请求都会输出分相耗时日志，流式响应末尾附带 SSE 注释行 `: agy-profile ...`（客户端会忽略），非流式响应带 `X-Agy-Profile` 响应头：

```
turn profile: model=gemini-3.1-pro-high new_session=1 models_lookup=0ms
acquire=16ms agy_boot=6312ms think=28094ms stream=7016ms total=41438ms
```

| 阶段 | 含义 | 优化手段 |
|---|---|---|
| `models_lookup` | `agy models` 查询（缓存命中应为 0） | 过期时自动"先回旧值、后台刷新" |
| `acquire` | 会话匹配/新建 | — |
| `agy_boot` | agy 进程冷启动（新会话才有，约 6s） | `PREWARM_MODELS` 预热 → 归零 |
| `think` | 模型思考（首字符前） | 换 flash 模型 / `--effort` 降档 |
| `stream` | 流式输出耗时 | 取决于模型输出速度 |

实测 `gemini-3.1-pro-high` 冷启动 41.4s 中：agy 启动 6.3s（可预热消除）、思考 28s、输出 7s；你看到的 65s 属于模型思考时长的正常波动（同 prompt 复测 18~50s 不等），代理自身开销 < 20ms。

## 项目结构

```
app/
├── config.py     # 环境变量配置
├── agy.py        # AgySession（常驻进程/NDJSON 读写）+ SessionManager + 模型发现
├── runner.py     # 回合执行器：会话复用、delta 去重、seed 拍平
├── store.py      # 控制台持久化：上游 API Key + agy 用量记录（单个 JSON）
├── auth.py       # 控制台密码 / 上游 API Key 两套鉴权 + /signin
├── dashboard.py  # 控制台数据与 API Key 管理接口
├── gemini_api.py # Gemini 路由与格式转换
├── openai_api.py # OpenAI 路由与格式转换
├── login.py      # 容器内 agy OAuth 登录桥（PTY + /login 页面）
├── update.py     # 容器内 agy 自更新桥（agy update + /update 页面）
└── static/       # 控制台页面 + 编译好的 app.css（构建产物，已入库）
src/              # React 页面、shadcn/ui 组件与主题
components.json   # shadcn New York / Radix 配置
vite.config.ts    # Vite 构建与开发代理
package.json      # 前端构建链（仅 devDependencies，运行时不需要 Node）
tests/            # fake_agy 协议模拟器 + 端到端测试
```
