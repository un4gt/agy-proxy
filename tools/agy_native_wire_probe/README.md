# CLI 原生网络层验证探针

当前部署入口见 [原生后端指南](../../docs/native-backend.md)。本目录的 `runtime.py`、`probe_gdb.py`、`observe.py`、`input_clean.py`、`symbols.py` 随镜像提供，其余文件为受控验收工具。下方早期实验步骤保留历史上下文；当前研究镜像基于 `v0.0.17-native`，不再依赖本机 `proto-fix` 镜像。

用于验证：真实 AGY CLI 保持原生 HTTP/TLS 发包，旁路读取其 TLS 明文，能否独立重组原始模型响应。

已验证捕获构建是 Linux amd64 AGY 1.4.0，二进制 SHA-256：

```text
60e72242f07c131f75afcd9a9e425d6ee097a1a1b3190ea98ae78fa4180bbfa8
```

镜像 `agy-proxy:proto-fix` 初始安装的是 1.3.5（哈希前缀 `b5385638`），真实运行会自动更新至上述构建。第一阶段文档曾误把初始版本号对应到更新后的哈希；现已更正，不能据此作严格同版本性能对比。

这是实验性原型，尚不作为生产默认后端。依赖该版本的 Go amd64 寄存器 ABI、函数符号表和栈检查指令形态。其他构建必须重新验证。

## 组成

- `symbols.py`：从实际 ELF 的 Go pclntab 和重定位信息定位 TLS Read/Write；不沿用 Windows 地址。
- `probe_gdb.py`：运行 CLI，在 TLS Write 栈检查之后保存请求缓冲区，返回时按实际写入长度提交；在 TLS Read 返回处观察响应。不修改收发数据，按 goroutine 关联参数与返回值。
- `observe.py`：解析 HTTP/1.1、HTTP/2/HPACK 和 SSE，区分 agent 与 checkpoint 请求。
- `run_probe.py`：执行原生与带探针的两轮相同提示，记录 stdout 事件时间与网络包。
- `fingerprints.py`：提取实际网络包中的 ClientHello，比较选定的非随机结构字段。
- `summarize.py`：校验正文、结束原因、用量、进程结束、捕获错误和握手结构。

HTTP 解析只覆盖本次观测到的形态。尚不完整支持 trailers、复杂 CONTINUATION、所有压缩/关闭连接结束的响应。Write 已结合返回值截取实际写入长度，但还需覆盖连接生命周期和更多并发时序。不能用于宣称全流量无损捕获。

## 复现

在 Windows PowerShell 项目根目录执行；命令会发送两次真实模型请求。测试模型固定为 `gemini-3.8-flash-low`，提示只要求返回固定标记，不要求执行工具。

```powershell
$probeDir = Join-Path (Get-Location) '.devdata/native-wire-repeat'
New-Item -ItemType Directory -Force $probeDir | Out-Null
Copy-Item tools/agy_native_wire_probe/*.py $probeDir
docker run -d --name agy-native-wire-repeat --cap-add SYS_PTRACE --security-opt seccomp=unconfined --entrypoint sleep -e HTTP_PROXY=http://host.docker.internal:7890 -e HTTPS_PROXY=http://host.docker.internal:7890 -v agyproto-gemini:/source-gemini:ro -v "${probeDir}:/evidence" agy-proxy:proto-fix infinity
docker exec agy-native-wire-repeat sh -c 'apt-get update -qq && apt-get install -y --no-install-recommends gdb binutils tcpdump'
docker exec agy-native-wire-repeat python -m pip install --target /tmp/probe-deps hpack==4.1.0
docker exec agy-native-wire-repeat python -c "from pathlib import Path; import shutil; src=Path('/source-gemini/antigravity-cli'); dst=Path('/root/.gemini/antigravity-cli'); dst.mkdir(parents=True,exist_ok=True); [shutil.copy2(src/name,dst/name) for name in ('antigravity-oauth-token','installation_id') if (src/name).is_file()]"
docker exec agy-native-wire-repeat python /evidence/symbols.py
docker exec agy-native-wire-repeat python /evidence/run_probe.py
docker exec agy-native-wire-repeat python /evidence/fingerprints.py
docker exec agy-native-wire-repeat python /evidence/summarize.py
```

按本地环境调整镜像、源卷和代理地址。源卷只读，凭据和安装标识复制进测试容器；没有 `CLOUD_CODE_URL` 替换，没有 Python 上游转发。HTTP CONNECT 代理与旧容器配置一致，TLS 仍由 CLI 建立。

原始 pcap 留在测试容器的 `/tmp`，不会复制进仓库。持久化的 hook 事件不记录 Authorization 值、OAuth 请求体、原始业务请求体或 cookie；会记录测试模型输出。只对无敏感内容的固定探针提示使用本脚本。

测试完成后清理自行创建的容器（不会删除源数据卷）：

```powershell
docker rm -f agy-native-wire-repeat
```

## 退出与判据

该构建在 GDB 继续跟踪进程退出时会触发调试器寄存器读取错误。探针在 `exit_group` 入口记录退出码、删除观察断点并 detach，由 CLI 正常退出。父进程另外检查 CLI 进程已消失；若子进程已被 GDB 回收，`reaped_inferior_exit` 为 null，不冒充取得了 waitpid 的退出状态。

`summary.json` 的 `passed` 表示本次有限场景的校验通过。它不等于所有网络指纹相同，也不覆盖多轮、并发、工具闭环、取消、异常重试或版本升级。

## 第二阶段：独立 session 的实时事件通道

- `runtime.py`：每个 session 私有 CLI 二进制快照、符号快照、工作目录、stdin/stdout/观测 FIFO、队列和回合锁；按请求 ID 过滤辅助请求。原始文本立即 yield，终结需同时收到网络完成与 CLI 成功结果。失败或取消销毁会话，不自动重试已交付内容。
- `sessions.py`：按可信认证层提供的 owner 与显式 session ID 绑定。不同 owner 即使 ID 相同也不复用；同一会话忙时拒绝重复占用；配置变化重建。限制容量，下一次申请时清理过期空闲实例。
- `live_sessions.py`：真实上游的并发双会话、多轮回忆与单边取消验证。
- `live_content.py`：同次调用的长文本时序、受限 finish 工具请求与模拟客户端结果回填；复用现有工具契约校验器。
- `summarize_sessions.py`：从测试证据导出断言和时序，检查原始 finish 内容与 CLI 结构化结果一致。

这组模块默认不启用。后续已新增可选 Chat Completions 后端，见 [pi 接入说明](../../docs/archive/native-chat-pi-20261018.md)。`function_call` 是内部观测事件，含 CLI 的 `finish` 与显示字段，不能直接当客户端工具事件发送。外部客户端不能指定可信 owner、cwd、CLI 参数或符号文件。直接使用原型的调用方应使用 `async with pool.lease(...)` 和 `aclosing(session.turn(...))`，确保断开连接后执行清理。

本地测试环境（实际会访问上游并消耗额度）：

```powershell
docker build -f tools/agy_native_wire_probe/Dockerfile -t agy-proxy:native-wire-dev .
$repoDir = (Get-Location).Path
$evidenceDir = Join-Path $repoDir '.devdata/native-wire-phase2-repeat'
New-Item -ItemType Directory -Force $evidenceDir | Out-Null
docker run -d --name agy-native-phase2-repeat --cap-add SYS_PTRACE --security-opt seccomp=unconfined -e HTTP_PROXY=http://host.docker.internal:7890 -e HTTPS_PROXY=http://host.docker.internal:7890 -v agyproto-gemini:/source-gemini:ro -v "${repoDir}:/repo:ro" -v "${evidenceDir}:/evidence" agy-proxy:native-wire-dev
docker exec agy-native-phase2-repeat python -c "from pathlib import Path; import shutil; src=Path('/source-gemini/antigravity-cli'); dst=Path('/root/.gemini/antigravity-cli'); dst.mkdir(parents=True,exist_ok=True); [shutil.copy2(src/name,dst/name) for name in ('antigravity-oauth-token','installation_id') if (src/name).is_file()]"
docker exec agy-native-phase2-repeat python /repo/tools/agy_native_wire_probe/symbols.py
docker exec agy-native-phase2-repeat sh -c '/root/.local/bin/agy --version > /evidence/binary-version.txt'
docker exec agy-native-phase2-repeat python /repo/tools/agy_native_wire_probe/live_sessions.py
docker exec agy-native-phase2-repeat python /repo/tools/agy_native_wire_probe/live_content.py
docker exec agy-native-phase2-repeat python /repo/tools/agy_native_wire_probe/summarize_sessions.py --evidence /evidence --output /evidence/session-summary.json
docker rm -f agy-native-phase2-repeat
```

若 CLI 自动更新导致二进制校验失败，先核对新版本，再重新生成符号并重跑；不要绕过哈希检查。原始测试输出只保存在忽略目录。不要将真实业务会话用于这些会持久化正文的 live harness。

离线测试无需 Docker、凭据或上游请求：

```powershell
uv run --with hpack pytest tests/test_native_wire_sessions.py tests/test_native_wire_observe.py tests/test_headless_sessions.py -q
```

本次结果与限制见 [第二阶段报告](../../docs/archive/cli-native-sessions-validation-20261018.md)。GDB 全停断点、协议解析覆盖度与工具参数增量适配仍需进一步处理。
