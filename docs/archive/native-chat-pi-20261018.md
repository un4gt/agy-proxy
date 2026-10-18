# 原生 Chat Completions 后端接入 pi

后续 CLI 内置提示和工具叠加的实测、清理开关与验证见 [原生输入清理验证](native-input-clean-20261018.md)。本文记录此前仅替换输出捕获链路的阶段。

## 路线与实现

优先以 `/v1/chat/completions` 接入 pi，保留项目现有的协议转换、认证、工具参数校验、session 历史匹配和 SSE 终结逻辑。CLI 负责真实上游网络请求，GDB 旁路观测 TLS 明文；外部工具、MCP 和 skills 在 pi 客户端执行。CLI 内只允许受限 agent 的 finish 提交。

CLI 私有路由需要继续研究，但不是 pi 当前接入的前置条件。它不能直接代替 pi 所需的 Chat Completions 协议，也不应把客户端 MCP/skills 转移到服务器执行。

新增 `AGY_PROXY_NATIVE_CHAT=true` 时，仅 Chat Completions 选择独立的 `NativeSessionManager`；其他协议继续走原有后端。默认关闭。`app/native_chat.py` 从原始网络 finish 参数构造输出，去掉 `toolAction`/`toolSummary`，要求它与 CLI 成功终结确认的结构化输出一致，再走原有 schema 校验。缺失、重复、错名或不一致的 finish 直接失败，原生后端关闭自动恢复重试。

启动必须确认 CLI 选择了 `proxy-external` agent。每个会话独立 CLI/GDB、管道、二进制与符号快照、工作目录和锁；继续沿用认证 owner + session 标识、历史与配置一致性检查。客户端断开、错误或候选不合法时销毁相应会话。

**这一版是功能接入，不是工具模式的逐 token 输出优化。** 现有 finish 模式本次只观测到完整参数，Chat SSE 会先开始并发心跳，工具/正文在完整捕获和校验之后交付。此前普通长文本的 11.81 秒首段提前量不能直接套用到这一模式。

## 本地真实验证

环境：Windows pi 1.1.0、Linux Docker 中 AGY 1.4.0、`gemini-3.8-flash-low`。pi 全局安装仍保留原版本；测试使用独立安装路径和独立配置、工作目录。MCP 使用官方 filesystem stdio server 1.0.0，限定到测试目录，没有访问远程业务 MCP。

最终三个 pi 进程同时运行，各用不同 session 标识：

| 场景 | 实际执行和独立验收 |
|---|---|
| 内置工具 | `read` 读取随机标记；`write` 创建结果；`edit` 修改计算值；`bash` 执行 Node 检查；最终文件内容匹配 |
| MCP | 实际调用 `mcp__filesystem__list_allowed_directories/read_text_file/write_file`，并回读结果；文件匹配随机标记与计算值 |
| Skills | pi 读取 `ledger-digest/SKILL.md`、相对路径 `references/contract.json`，编写实现并通过 skill 的 Node 检查器；独立复验通过，skill/参考文件/检查器哈希未改变 |

三组最终运行均无工具错误、无 assistant 错误，退出码 0。相关离线和回归测试 **182 项通过**，包括新后端选路、原始输出一致性、owner/session 隔离、取消清理及既有统一执行器测试。

原始受控证据保存在忽略目录 `.devdata/native-chat-pi-20261018`。机器摘要见 [验证结果](../reference/native-chat-pi-20261018.json)。本轮没有操作线上部署。

## 初次失败与修正

- pi 的 API key 配置需要 `${ENV_NAME}` 插值。直接填变量名被当成字面量，首轮产生 401；示例已经修正。
- 早期真实多轮出现间歇性 chunk size 解析错误，不能只以已产生文件判定成功。探针原先在 TLS Write 入口立即记录传入字节；现改为入口保存、返回时按实际返回长度提交，避免把未成功写入的数据当作有效请求。补充了零写入、部分写入的离线测试，修改后的并发复测通过。未取得早期错误的完整原始网络序列，因此仍不将这轮复测视为所有分块错误已经根治的证明。
- 一次 npx 启动超时尚未进入请求阶段。最终验收直接使用已安装的 pi CLI JS 入口，避免 npm 启动行为混入协议验证。失败证据保留在本地目录。

## 运行方式

普通 Dockerfile 不带 GDB，本功能需探针镜像。先在本地构建 `tools/agy_native_wire_probe/Dockerfile`，使用独立容器和只读源凭据卷，参照 [探针步骤](../../tools/agy_native_wire_probe/README.md) 初始化凭据。不要复制或输出凭据到仓库。

容器启动配置需要：

```text
--cap-add SYS_PTRACE --security-opt seccomp=unconfined
-p 127.0.0.1:18317:8317
AGY_PROXY_NATIVE_CHAT=true
AGY_PROXY_NATIVE_SYMBOLS=/evidence/symbols.json
AGY_PROXY_BIN=/root/.local/bin/agy
AGY_PROXY_API_KEYS=<本地测试密钥>
```

确认 CLI 版本后，在容器中运行 `python /app/tools/agy_native_wire_probe/symbols.py` 生成匹配符号，再从 `/app` 启动：

```sh
python -m uvicorn app:create_app --factory --host 0.0.0.0 --port 8317
```

开发时也可以将当前仓库只读挂载到 `/repo`，从 `/repo` 执行以上命令。CLI 自动更新后需重新核对版本并生成符号；哈希不符会拒绝启动捕获。

pi 配置示例位于 [examples/pi-native](../../examples/pi-native/models.json)。复制到独立的 pi 配置目录，并在启动进程中设置 `PI_CODING_AGENT_DIR` 与 `AGY_NATIVE_API_KEY`：

```powershell
$env:PI_CODING_AGENT_DIR = '<独立 pi 配置目录>'
$env:AGY_NATIVE_API_KEY = '<与本地服务一致的密钥>'
pi --provider agy-native --model gemini-3.8-flash-low --thinking off
```

示例没有把固定 session ID 写入全局 provider 配置，避免多个聊天误用同一标识；无显式标识时使用现有按 owner 隔离的唯一历史续接。需要显式 affinity 时，应由每个 pi 会话独立设置 `X-Agy-Session-Id`，同一会话延续时保持不变。

使用 pi 1.1.0 的 `mcp.json` 配置 MCP，skills 使用 pi 的 skill 发现/显式路径功能，均位于客户端。不要把客户端 MCP 配置或 skill 文件放入 CLI 的服务器工作目录。

复现受控验收：

```powershell
uv run python tests/pi_native_smoke.py --scenario tools --pi-entry '<pi CLI JS 路径>'
uv run python tests/pi_native_smoke.py --scenario mcp --pi-entry '<pi CLI JS 路径>' --filesystem-server '<官方 filesystem MCP dist/index.js 路径>'
uv run python tests/pi_native_smoke.py --scenario skills --pi-entry '<pi CLI JS 路径>'
```

脚本默认连接本地 18317 端口；可用 `--base-url` 覆盖。密钥通过 `NATIVE_PI_API_KEY` 环境变量注入。脚本使用隔离目录，保留完整受控事件与独立验收结果，不修改用户全局配置。

## 边界

仍是实验性 Linux amd64/GDB 后端，不建议直接替换线上默认后端。暂不接受图片；尚未覆盖远程 MCP OAuth、所有 MCP servers、长时间压力、多模型、CLI 升级与异常网络的全部情况。观测器仍有 HTTP trailers、压缩 SSE、连接生命周期等覆盖限制；GDB 会改变时序。原生后端独立会话池尚未汇总进 dashboard 的会话计数。

下一步优先处理结构化 finish 的增量交付与更低开销的可靠观测，再考虑 CLI 私有路由/请求注入能力。
