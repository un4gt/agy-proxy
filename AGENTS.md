# Repository Guidelines

## 项目结构与模块职责

- `main.py` 启动 FastAPI；`app/` 包含协议适配、CLI 会话、账号隔离、用量和存储逻辑。
- `src/` 是 React 19 + TypeScript 控制台；`src/components/ui/` 存放 shadcn/ui 组件，`src/lib/` 存放共享逻辑。
- `app/static/` 是随仓库分发的前端产物；修改前端后重新构建，并在 PR 中包含相关产物。
- `tests/` 包含单元测试、模拟 CLI 和客户端验收脚本；`tools/agy_native_wire_probe/` 是原生协议探针。
- `docs/` 存放开发、配置和部署指南；`examples/` 提供客户端配置示例。

## 构建、测试与本地开发

需要 Python 3.12+、uv；前端需要 Node.js 和 npm。真实 API 请求还需要已登录的 agy CLI。

| 命令 | 用途 |
| --- | --- |
| `uv sync` | 安装 Python 及开发依赖 |
| `uv run main.py` | 启动服务，默认 `127.0.0.1:8317` |
| `uv run pytest` | 运行 Python 测试 |
| `npm ci` | 按锁文件安装前端依赖 |
| `npm run dev` | 启动 Vite，接口代理到本机 8317 |
| `npm run build` | TypeScript 类型检查并构建到 `app/static/` |
| `node --test tests/account-models.test.ts` | 运行模型分组测试；需支持直接运行 TypeScript 的 Node.js |

## 代码风格与命名

Python 使用四空格缩进、类型注解和 `snake_case`；类名使用 `PascalCase`。TypeScript 使用两空格缩进、双引号、分号；React 组件使用 `PascalCase`，函数使用 `camelCase`，组件文件沿用 `kebab-case.tsx`。遵循邻近代码风格，避免无关格式调整。当前未配置统一格式化或 lint 命令；前端启用 TypeScript strict 检查。

## 测试规范

Python 使用 pytest，文件命名 `tests/test_*.py`，测试函数命名 `test_*`。优先使用 `fake_agy.py`、临时目录和模拟凭据；协议单元测试不需要真实账号。可用 `uv run pytest tests/test_images.py -q` 定向验证。

目前没有强制覆盖率门槛。行为修复应覆盖触发条件及关键失败分支，尤其是流式输出、工具调用、会话隔离和用量计算。真实客户端的 `*_smoke.py` 需要对应环境；报告中区分模拟测试与真实上游验证。界面变更同时检查桌面和移动布局。

## 提交与 Pull Request

沿用 Git 历史的 Conventional Commits：如 `fix(native): ...`、`feat(accounts): ...`、`docs: ...`；破坏性变更使用 `!`。每个提交聚焦一个逻辑改动。

PR 描述说明问题、修改后的行为、验证命令与结果；有对应 issue 时附链接。界面变更附截图，配置或协议变更同步更新文档；发布变更记录在 `docs/releases/`。

## Docker 更新流程

用户说“更新”时，默认按以下流程执行：

1. 完成相关测试和前端构建，将代码及 `app/static/` 产物打包为新的 Docker 镜像；使用明确的新版本标签，推送到镜像仓库（如 `tumuer/agy-proxy:<新版本>`），确认线上可以拉取。
2. 线上只修改现有 Compose 配置中生效的 `image:` 版本，保留项目名、环境变量、端口、数据卷和其他运行配置。使用覆盖文件时，修改最终生效的镜像声明。
3. 在原部署目录拉取镜像，并重建启动容器：

   ```bash
   docker compose pull agy-proxy
   docker compose up -d --no-build agy-proxy
   ```

   命令须沿用原部署的 `-p` 和全部 `-f` 参数；例如原生主服务使用 `-f docker-compose.yml -f docker-compose.native-primary.yml`。更新镜像需要 `up -d` 重建容器，单独执行 `restart` 不会切换镜像。
4. 检查容器状态、`/health`、实际镜像版本及关键接口；确认原有账号、密钥和用量数据保留。记录上一版本，回滚时恢复其 `image:` 标签，再执行相同的拉取和重建命令。

正式更新通过已发布镜像交付，不在服务器构建镜像、复制源码补丁或直接修改运行中容器。

## 配置、安全与协作

源码启动读取进程环境变量，不自动加载 `.env`；配置项见 `docs/configuration.md`。密钥、账号凭据、私有会话和临时验证脚本留在忽略的 `.devdata/`，不要写入提交或日志。部署前备份并保留数据卷。

代理默认用中文沟通，修改前先分析相关调用链；保留已有未提交改动，并遵守用户对提交及部署范围的约定。
