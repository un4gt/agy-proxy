# 部署与运维

[返回 README](../README.md) · [环境变量](configuration.md) · [多账号隔离](account-isolation.md)

## Docker 部署

首次启动见 [README 快速开始](../README.md#docker-快速开始)。镜像发布于 `docker.io/tumuer/agy-proxy`，当前 Compose 固定为 `v0.0.15`，镜像构建会校验 agy CLI 为 `1.3.3`。运行环境无需安装 Python、Node 或 agy。

服务器上保留 `docker-compose.yml` 和由 `.env.example` 创建的 `.env` 即可部署。至少设置 `AGY_PROXY_PASSWORD`；需要代理访问 Google 时配置 `AGY_PROXY_HTTP_PROXY`。查看启动状态：

```bash
docker compose ps
docker compose logs -f
curl http://127.0.0.1:8317/health
```

`/health` 返回状态、版本及是否配置鉴权和登录；`/docs`、`/openapi.json` 已关闭。

也可使用 `docker run`，适用于新部署：

```bash
docker run -d --name agy-proxy -p 8317:8317 \
  --env-file .env \
  -v agy-gemini:/root/.gemini \
  -v agy-data:/root/.agy-proxy \
  --add-host=host.docker.internal:host-gateway \
  --restart unless-stopped \
  tumuer/agy-proxy:v0.0.15
```

Compose 默认给卷名加项目名前缀，如 `agy-proxy_agy-data`；上例的 `agy-data` 是另一个卷。从 Compose 切换部署方式时，应显式复用原卷。

## 鉴权与控制台

打开 `http://服务器IP:8317` 后，通过 `/signin` 登录控制台；登录 Cookie 有效期 7 天，退出使用 `/signout`。

| 凭据 | 行为 |
|---|---|
| `AGY_PROXY_PASSWORD` | 控制台与管理接口密码；客户端建议使用独立 API Key |
| `AGY_PROXY_API_KEYS` | 环境变量中的固定 Key，可调用模型 API，也可登录控制台 |
| 控制台创建的 API Key | 仅用于模型 API，可单独改名、吊销及统计用量，不能登录管理页面 |

未单独设置密码时，第一把固定 Key 作为面板密码兜底。当前模型 API 也接受有效面板凭据及登录 Cookie；建议为客户端创建专用 Key，便于单独管理。密码与固定 Key 均未配置时，服务不启用鉴权，适用于本机开发。

API 支持 `Authorization: Bearer <key>`、Basic（`curl -u :key`）、`x-api-key`、`x-goog-api-key` 和 `?key=`。控制台生成的 Key 仅在创建时完整展示；固定 Key 不出现在该列表中。

控制台提供：

- 累计请求、输入/输出/思考/缓存命中 Tokens；用量来自 agy 的 `result.usage`。
- 实时模型列表、按模型用量，以及按基础模型启用或禁用整个变体组。
- API Key 创建、改名、吊销与按 Key 用量统计。
- 最近 500 条请求记录；更早请求保留累计统计，首次部署前的历史不可追溯。

控制台数据保存在 `AGY_PROXY_DATA_DIR/data.json`，页面每 5 秒刷新。管理接口包括 `GET /dashboard`、`GET /api/models`、`POST /api/keys`、`PATCH|DELETE /api/keys/{id}`。

页面顶部品牌旁显示后端 `/about` 返回的 agy-proxy 部署版本；CLI 版本在更新页面单独显示。

## 添加账号与授权登录

在「账号详情」（`/login`）添加账号，可导入 agy 原生 `antigravity-oauth-token` 或含 `token` 的 agy-switch 完整账号 JSON，也可先创建空账号，再在该账号卡片中授权。交互登录桥需要 Linux / Docker。

1. 在目标账号卡片中开始连接，用终端画面的方向键和回车选择登录方式、确认选项；支持空格、Tab、Esc 和 Y/N。
2. 点击「打开 Google 授权页」，在自己的电脑浏览器中完成授权。
3. 从授权回调页面或地址栏的 `code=` 参数复制授权码，粘回账号登录弹窗并提交。
4. 根据 CLI 提示完成确认，最后点击「完成并关闭」。凭据写入该账号的持久化目录。

登录桥不会自动确认 CLI 选项。点击「取消本次登录」可结束登录进程；整次登录超时为 15 分钟，授权码失效时按 CLI 提示重新获取。

「导入本机当前凭据」读取服务器的原生登录文件。首次添加新账号时，已有文件凭据先迁移为「原服务账号」，保留原安装标识；后续每个账号拥有独立凭据、状态和固定 `installation_id`。未启用隔离账号时，仍可使用原本的本机登录方式。

账号卡片显示身份、套餐、连接状态与检查时间。「查看 Usage」查询 Gemini、Claude/GPT 等模型组的额度，并支持刷新。额度来自 Cloud Code 查询，正常缓存 60 秒、失败缓存 10 秒；查询失败或未知值不会显示为 0%。接口为 `GET /api/accounts`、`POST /api/accounts`、`GET /api/accounts/{id}/usage?refresh=true`；`current` 仅用于尚未启用隔离账号的旧模式。

账号查询模块刷新过期令牌需要配置匹配的 `AGY_PROXY_OAUTH_CLIENT_ID` 和 `AGY_PROXY_OAUTH_CLIENT_SECRET`。该模块的刷新结果仅保存在内存中，CLI 自身的凭据管理独立。配置含义见[环境变量](configuration.md)，账号绑定与耗尽后切换规则见[隔离说明](account-isolation.md)。

隔离账号卡片右上角提供编辑名称和删除操作。重命名保留原凭据、安装标识与绑定；删除需确认，会移除该账号的本地数据并解除其客户端绑定，后续请求重新选择可用账号。正在处理请求或登录的账号会拒绝删除，需等待请求结束或完成/取消登录。删除最后一个账号后保持无账号状态，不回退到旧的本机凭据。管理接口为 `PATCH /api/accounts/{id}`（`{"name":"新名称"}`）和 `DELETE /api/accounts/{id}`，均要求面板鉴权。

## 升级与数据持久化

两个目录均需持久化并备份：

| Compose 卷 | 容器路径 | 保存内容 |
|---|---|---|
| `agy-gemini` | `/root/.gemini` | 原本的本机登录凭据和 CLI 状态 |
| `agy-data` | `/root/.agy-proxy` | API Key、用量、模型设置、隔离账号凭据、固定身份和客户端绑定 |

`docker compose pull` 只拉镜像，不更新服务器上的 Compose 文件。升级前确认配置包含两个挂载和对应的顶层卷声明，并检查实际运行容器：

```bash
docker inspect agy-proxy --format '{{json .Mounts}}'
```

输出应包含上述两个容器路径。保持部署目录、Compose 项目名与卷名一致；多个代理实例使用各自的数据目录。

若旧部署缺少挂载，先停止服务，导出完整数据与部署配置，再迁移；已有多账号时只备份 `data.json` 会遗漏凭据、身份与绑定。以下为 Bash 示例：

```bash
docker compose stop
agy_backup_dir=$(mktemp -d /tmp/agy-proxy-backup.XXXXXX)
docker cp agy-proxy:/root/.agy-proxy "$agy_backup_dir/agy-data"
docker cp agy-proxy:/root/.gemini "$agy_backup_dir/agy-gemini"
cp .env docker-compose.yml "$agy_backup_dir/"
```

备份包含密钥和凭据，保存在受限目录。确认成功后补齐挂载，并在服务停止时将两份数据目录内容分别恢复到对应卷。已有正确挂载的部署，备份后修改镜像 tag 并执行：

```bash
docker compose pull
docker compose up -d
```

不要用 `docker compose down -v` 升级，它会删除数据卷。修改 `.env` 后也执行 `docker compose up -d`；仅 `restart` 不更新容器环境变量。

**升级后密钥与用量为空：** 先检查实际挂载、`data.json` 和日志中的 `loaded ... api key(s)`、`could not read`、`could not persist`。读取失败也可能表现为空列表；先备份和排查，再创建新数据。若旧容器已删除且无备份，重新拉取镜像无法恢复历史用量。客户端仍保存的旧 Key 可暂时加入 `AGY_PROXY_API_KEYS` 恢复鉴权，但不会恢复历史统计。

## 容器内更新 agy CLI

点击页头的 agy 版本徽章或访问 `/update`，选择「检查并更新」。代理执行官方 `agy update` 并展示进度；完成后新会话使用新版本。

更新前先结束空闲会话，进行中的请求继续使用原版本，结束后重建。`AGY_PROXY_HTTP_PROXY` 同样适用于更新进程。容器重新创建后恢复镜像内置的 CLI 版本；升级 CLI 后应重新核对[已验证的隔离行为](account-isolation.md)。相关接口为 `GET /update/status`、`POST /update/start`，使用控制台鉴权。
