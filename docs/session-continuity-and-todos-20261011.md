# 无客户端改动的 AGY 会话复用与任务状态排查

排查日期：2026-10-11，北京时间。客户端为 dsh desktop；归档为 `test_sessions/dsh-session-session-65d6df61-fe5b-40ad-9518-d6220db25ce1.zip`，已解包为同名目录下的 `session.v4.jsonl`。

原始会话归档和解包日志仅保留在本地，不纳入 Git；本文引用的脱敏证据位于 `docs/reference/`。

## 为什么三步各自启动了 AGY

初版实现仅在识别到显式会话 ID 时构建 SessionContext。没有识别到会话字段时，成功返回也会销毁进程。因此，虽然协议和进程管理已经支持 stdin 多轮，这次 dsh 默认请求仍然走了每请求新建进程的路径。这是代理实现未覆盖默认客户端用法，并不意味着 AGY 不支持多轮。

服务器运行日志、AGY conversation 数据库的 user_input 和公开 finish 工具参数，与客户端 session 时间相互对应：

| 客户端步骤 | 北京时间结束 | PID | AGY conversation | 服务端总耗时 | 交付客户端 |
| --- | --- | ---: | --- | ---: | --- |
| 1 | 15:32:09 | 8438 | 63da35cf-fa74-4477-950b-be860c870361 | 21.249 秒 | todo_write、glob |
| 2 | 15:35:17 | 8477 | dcdd676b-b621-4ed1-8f22-262685c4655c | 186.337 秒 | todo_write、write |
| 3 | 15:39:24 | 8531 | 0bf9113b-ab29-4d04-8688-7ac23b1bfac1 | 245.364 秒 | 最终文本，没有工具调用 |

三步均为 AGY SUCCESS、代理 status=ok、new_session=1。它们分别带 4、7、10 条消息，均有相同的 27 个工具定义，后一步包含前一步的 assistant/tool 消息。独立 PID 8425 是标题生成，不属于上述三步。此次没有 504，也没有证据表明 SSE 中断导致任务状态丢失。

## 服务端自动维护会话

成功生成后保留 PID、conversation、配置指纹和已交付历史指纹。请求到达时：

1. 有现成的明确会话字段时，按该字段和 API 凭据隔离。
2. 没有明确会话字段时，在相同 API 凭据的自动会话中查找：完整历史必须包含上一轮完整输入和实际交付的 assistant 回复；工具调用 ID、参数与内容也必须匹配；后面必须有新消息。
3. 唯一匹配且 model、effort、工具、schema、附件一致时，复用原 PID/conversation，只向 stdin 传入新增消息及上一条 assistant 的工具 ID 回显。
4. 首次请求、历史改写/截断、配置变化、回收或歧义时创建独立进程。并发分支互相隔离，单个 PID 不交错执行两轮。已在执行的同一续接重复请求返回 409。

这条路径是默认行为，dsh/pi 和其他 agent 均不需要新增 `X-Agy-Session-Id` 或修改缓存配置。只凭相同用户名、API Key 或第一条 prompt 不会合并会话。完全相同的克隆如果没有任何身份字段，线上无法区别其来源；多个匹配候选时重建，不随机选择。空闲 TTL/LRU、失败和取消的回收规则仍适用。

日志新增 `affinity=history` / `affinity=explicit`，并在 bound/reused 行记录内部会话标识、PID 和 conversation，便于判断请求是否连续复用。

## 截图中的任务列表为何没更新

客户端归档共有两条 `todo/write` 快照：

| 事件 seq | 时间 | 三项状态 |
| --- | --- | --- |
| 18 | 15:32:09.989 | in_progress、pending、pending |
| 26 | 15:35:17.513 | completed、in_progress、pending |

之后 write 返回了 Created file。最后一步在 15:39:24.179 收到 `stopReason=stop`，随后 seq 35 的 turn/end 为 completed；没有第三次 todo_write，没有 read/bash 等验证调用。最后一条最终答复为 963 字符，SHA-256 为 `65f026be7a920c11771b93d665aedb3e044a0c22092124a6ae078cb8df1adf77`，与服务器公开 finish.content 一致；finish.kind=message，tool_calls=[]。代理没有丢掉一个已经生成的任务更新。

dsh 的 `packages/todo/tool-todo/src/index.ts` 明确按最后一条 todo/write 展示任务，turn/end 不修改它。因此截图“1 已完成、1 进行中、1 待处理”与日志完全一致：模型在未同步任务列表、未执行计划中的验证时直接结束了本轮。

已在通用工具桥接提示中补充：一次工具成功只代表该操作成功；继续剩余实现和验证；如果建立了任务清单，须通过客户端提供的任务工具同步实际进展后再给最终答复；没有真实验证结果不能把验证写成完成。代理不伪造 todo_write，也不将所有任务自动标为完成。这是模型行为约束，不能把提示改动当作确定性的任务完成保证。

## 验证记录

全量回归：`uv run pytest -q -o faulthandler_timeout=30`，**365 passed、2 skipped**，34.01 秒。覆盖不带会话头的三轮同 PID、Chat/Responses/Messages/Gemini/结构化接口、流式切换、凭据隔离、历史分支和歧义、重试、配置改变、空闲回收及附件隔离。

服务器隔离镜像的模拟 CLI 测试通过，涵盖凭据隔离、usage 差分和 SSE 心跳。

真实 CLI 三轮测试不携带任何自定义会话头：PID 28，conversation `6c1e2465-47da-4f17-aaa6-39ef17e2cb7c`，三轮始终相同；后两轮分别约 4.10、3.05 秒，new_session=0。第一轮请求客户端 read，第二轮处理工具结果，第三轮处理后续用户消息。

原第三步输入重放保持 10 条消息、27 个工具定义及 gemini-3.8-flash + high，仅使用更新后的桥接提示。21.91 秒返回 todo_write 和 pwsh：三项状态为 completed、completed、in_progress；pwsh 请求提取 index-edge.html 中的 JavaScript 并检查语法。工具请求已通过参数校验，未执行任何客户端命令，不能将其当作游戏已验证。该次生成使用新会话，不能把其改善归因于 PID 复用；一次成功也不能保证模型以后不会遗漏任务更新。

生产 HTTP 验证同样没有会话头：PID **256**，conversation `c2fdbec1-50fe-4998-8321-fd5672da5327`，三轮 new_session=[1, 0, 0]，stream=[false, false, true]，总耗时 17.07 秒。日志记录 affinity=history，确认自动关联在实际部署接口生效。

2026-10-11 16:38（北京时间）已部署镜像 `agy-proxy:server-sessions-20261011`，镜像 ID `sha256:543468fccb4eaf159f35378772d62d89ea6d27c1e944705c6e667737af7d7dc8`。容器 healthy，34 个部署文件哈希核对通过；登录、4 把 API Key、用量、模型禁用配置和原有数据卷保留。回退备份位于 `/home/docker_depolyments/agy-proxy/backups/server-sessions-20261011`。

原始问题的脱敏协议证据：[session-continuity-evidence-20261011.json](reference/session-continuity-evidence-20261011.json)。部署、三轮复用和重放结果：[server-sessions-verification-20261011.json](reference/server-sessions-verification-20261011.json)。原客户端已结束的那一轮不会由代理回填 TODO；此修正用于后续请求。
