# 移除代理提示字符上限（2026-10-13）

按用户要求，删除代理对消息历史和工具定义的 **200,000 字符检查**。所有模型、Chat Completions、Responses、Messages、Gemini，以及 Messages token 估算接口统一生效。模型上下文容量交给 AGY 与上游处理，代理不截断历史，也不修改 Pi 的 `contextWindow`。

## 实现及部署

删除 `Config.max_prompt_chars`，移除 `ImageInput.prepare()` 中的长度判断及参数，更新共享执行入口和 Messages token 估算入口。HTTP 请求体 32 MiB 及现有图片资源限制保留。

本次运行代码只涉及 `app/config.py`、`app/images.py`、`app/protocol_common.py`、`app/anthropic_api.py` 四个文件。部署前确认服务器文件与修改前本地副本的 SHA-256 一致，再基于正在运行的镜像构建仅替换这四个文件的新镜像。

- 运行镜像：`agy-proxy:no-prompt-cap-20261013`。
- 镜像 ID：`sha256:390d95e3402910669a8833bcaf2939136f483e746510ef31075fcd44a8b93ba9`。
- Compose 项目与目录：`agy-proxy`，`/home/docker_depolyments/agy-proxy`。
- 容器健康检查通过；部署前确认没有运行中的 AGY 子进程。
- Compose 备份：`docker-compose.yml.before-prompt-cap-removal-20261013`。
- 旧镜像另标记为 `agy-proxy:before-prompt-cap-removal-20261013`，原账号和数据卷保留。

回滚时在部署目录恢复上述 Compose 备份，再运行 `sudo docker compose --project-name agy-proxy up -d --no-build --pull never agy-proxy`。备份恢复后的旧镜像包含原字符限制。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| 修改前的新增长输入用例 | 稳定复现 HTTP 413 |
| 新增长输入回归 | 21 项通过，覆盖四协议流式／非流式、长文本、长工具结果、长工具定义及 token 估算 |
| 完整测试套件 | 503 通过，2 项原有跳过 |
| 原 Pi 会话的三份重建请求 | 194,827／211,062／211,100 字符均通过输入及工具历史校验 |
| 真实普通长文本，224,698 字符 | HTTP 200，返回 202 字符回答，`finish_reason=stop`、`[DONE]`，完整记录 usage |
| 真实边界标记测试，216,493 字符 | AGY 返回 SUCCESS，代理随后因既有回合编号校验返回流内错误 |

两次真实请求均使用 `gemini-3.8-flash` 和 `reasoning_effort=high`；服务端解析为 `gemini-3.8-flash-high`。所有内容均为合成文本，没有重放用户历史工具操作。

普通长文本请求耗时 26.844 秒，关联 ID 为 `agy_1329b202e9134071a937f19db3b138dd`。AGY 返回了对合成文档的摘要，未严格返回测试所要求的固定字符串。因此，这项验证证明长请求可以正常处理和结束；严格的固定应答断言未通过，不能标为全部真实验收通过。

## 单独发现的既有问题

首个测试关联 ID 为 `agy_a8c48fb9b86b46e4b153ebd31304ec09`，最终错误为 `cli_protocol_error: agy result does not match the active turn number`。服务器日志确认 AGY 本身返回 `SUCCESS`。

该会话的 AGY transcript 显示：模型先直接回答，CLI 随后追加“如果任务已完成请调用 finish”的内部 USER_INPUT 提醒，模型再调用 finish。代理目前要求 `result.num_turns` 严格等于自己写入 stdin 的请求次数；这种内部提醒带来的计数差异需要单独核对协议兼容性。本次没有改动回合编号校验或自动恢复行为。

首个测试未正常完成，第二个测试正常完成但未命中固定应答。两个结果均保留在[脱敏证据](../reference/prompt-cap-removal-20261013.json)，没有通过反复重试隐藏失败。关于最初 Pi 无输出问题，见[故障分析](pi-empty-output-analysis-20261013.md)。
