# 原生完成判定与控制台更新（已发布）

2026-10-28 整理的以下变更已随 [v0.0.17-native.10](v0.0.17-native.10.md) 发布。
本页保留提交前的验证范围；部署结果以该版本发布记录为准。

## 本次新增内容

- 原生 finish 判定限定在当前输入轮次；内部补交计数不跨用户轮次累计，结构化
  输出不复用历史结果。偶发缺少 finish 时由原 CLI executor 在本轮内补交，最多
  三次。耗尽后保留用量并返回错误，不再外层重启重放。
- 控制台更新概览、账号、登录和 CLI 更新页的布局、主题、表格及弹窗密度；支持
  桌面与移动尺寸。页面切换保留已加载数据，延迟显示骨架屏，减少加载闪烁。
- 账号 Usage 按基础模型合并思考档位，显示共享额度；不同模型不会因额度相同而
  被合并。账号目录使用匹配 CLI 版本的客户端标识，保留当前可用模型档位。
- 前端源码与重新构建的 `app/static/app.css`、`app/static/assets/ui.js` 一同提交。
  `Dockerfile` 通过复制 app 带入产物，`Dockerfile.native` 显式复制当前产物；
  native.10 已包含本次前端，没有沿用 native.9 的旧静态资源。

## 一并整理的既有修复

补齐 native.4–native.9 的源码、测试与发布记录：大输入保护、CLI 内部提醒轮次
计数、HTTP/2 捕获连接状态、原生上游用量记账、白名单图片读取、上游 HTTP 错误
识别与配额错误传递。历史版本的具体发布范围仍以各自发布记录为准。

## 验证

- `uv run pytest -q --tb=short`：777 passed、2 skipped；跳过项需要 Linux PTY。
- Node 22 下运行 `node --test tests/account-models.test.ts`：6 passed。
- Node 22 下运行 `npm run build`：TypeScript strict 检查与 Vite 构建通过。
- 使用构建后的静态资源和模拟 API 检查 1440×1000、390×844 两种尺寸：概览、
  账号、Usage 弹窗、登录、CLI 更新共十个视图，无页面脚本错误和页面横向溢出。
- 本地真实固定 CLI 与模拟上游：七个 finish 边界场景通过；完整 Chat API 链路
  五个客户端请求、四次内部补交均通过，保持同一会话、无旧结果或重复工具调用，
  每个请求 attempts=1、repair_attempts=0，用量完整。复验脚本为
  `tests/native_finish_boundary_smoke.py`、`tests/native_finish_client_smoke.py`。

finish 验收为隔离模拟上游，不代表真实上游长会话验收。内部补交仍增加模型调用
与等待；不能据此宣称上游 ANY 约束已修复，或已经移除 stdin/stdout、GDB 依赖。

研究报告、原始会话、截图和临时实验材料保留本地，不纳入此次提交或发布镜像。
部署应先构建并验证新版本镜像，推送后仅更新原 Compose 的生效 image，沿用原
项目名、全部配置文件及数据卷。操作步骤见[部署指南](../deployment.md)。
