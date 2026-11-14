# 多账号隔离与仅额度耗尽时切换

## 参考源码与 CLI 实测

参考仓库：[`anglee0323/agy-switch`](https://github.com/anglee0323/agy-switch)，检查版本 `2d975543d81e8977da666782c762407309c7d6f1`。DeepWiki 未收录该仓库，使用 GitHub MCP 读取模型和凭据代码，并核对同一提交的源码。

| 参考位置 | 已核实的机制 |
|---|---|
| [`models/account.rs`](https://github.com/anglee0323/agy-switch/blob/2d975543d81e8977da666782c762407309c7d6f1/src-tauri/src/models/account.rs) | 每个账号保存可选 `device_profile`，包括 `machine_id`、`mac_machine_id`、`dev_device_id`、`sqm_id` |
| [`modules/account.rs`](https://github.com/anglee0323/agy-switch/blob/2d975543d81e8977da666782c762407309c7d6f1/src-tauri/src/modules/account.rs#L1089) | 合并切换状态时，仅在账号没有指纹的情况下生成并保存，不在每次切换时重新生成 |
| [`modules/device.rs`](https://github.com/anglee0323/agy-switch/blob/2d975543d81e8977da666782c762407309c7d6f1/src-tauri/src/modules/device.rs#L100) | 写 IDE `storage.json` 的嵌套与扁平 telemetry 字段，并把 `dev_device_id` 同步到 `state.vscdb` 的 `storage.serviceMachineId` |
| [`modules/cli_credentials.rs`](https://github.com/anglee0323/agy-switch/blob/2d975543d81e8977da666782c762407309c7d6f1/src-tauri/src/modules/cli_credentials.rs) | 原生 CLI 凭据位于 `~/.gemini/antigravity-cli/antigravity-oauth-token`，原子替换；不是通用 Gemini CLI 的凭据文件 |
| [`modules/integration.rs`](https://github.com/anglee0323/agy-switch/blob/2d975543d81e8977da666782c762407309c7d6f1/src-tauri/src/modules/integration.rs#L193) | CLI 文件同步与操作系统凭据库有区别；CLI-only 切换只处理文件 |

此版本参考仓库没有“CLI `state.json` 中的机器指纹切换”实现。IDE 使用 `storage.json` / `state.vscdb`；其 `auto_switch_state.json` 记录自动切换暂停/失败状态，也不是机器指纹。代理借鉴的是“账号拥有固定身份并与凭据一起持久化”的机制，按 Python/CLI 架构独立实现，没有移植 IDE 数据库写入代码。

2026-10-12 在 Windows 的 agy **1.3.3** 上使用临时目录验证：

1. 仅设置独立 `HOME` / `USERPROFILE`，即使目录中放了无效原生 token，`agy models` 仍可成功；CLI 日志显示从共享系统凭据库认证。目录隔离本身不足以隔离凭据。
2. 启用 CLI 的 SSH 环境模式后，日志明确显示 `Using file-based token storage because SSH session detected`；同一无效 token 得到 401，未回退到系统凭据库。
3. CLI 原生安装标识文件为 `.gemini/antigravity-cli/installation_id`，内容是 UUID。代理为每个账号预先生成并持久化该 UUID，同时隔离 `.gemini/state.json`、`jetski_state.pbtxt`、对话数据库和缓存。

SSH 环境标记只用于所启动子进程，配合独立的 HOME / USERPROFILE / APPDATA / LOCALAPPDATA / XDG / 临时目录。会清除可覆盖账号认证的继承环境变量（ADC、直接 token、Gateway、已有 Language Server 连接等）。不修改代理进程环境或宿主机系统凭据库。该文件模式行为已验证至上述 CLI 版本；升级 CLI 后应重新验证，不能假设未来版本保持内部行为。

## 数据结构

```text
DATA_DIR/
  accounts.json                    账号索引、API Key 的散列绑定、额度耗尽状态
  accounts/<id>/
    state.json                     固定 machine_id（UUID）及 account_id
    home/
      .gemini/
        state.json                 该账号自己的 CLI 状态；不伪造 IDE telemetry 字段
        installation_id            固定 UUID
        antigravity-cli/
          installation_id          同一个固定 UUID，CLI 实际使用
          antigravity-oauth-token   仅该账号的原生 OAuth 凭据
          ...                      缓存、日志、会话等
```

新账号的 UUID 相互独立，同账号的已有 `state.json` 不会因重启或重新导入同一 refresh token 而重置。重新导入也会核对 CLI 实际保存的凭据，因此原生登录或令牌刷新后的当前凭据仍复用原账号目录。首次迁移本机账号尽量沿用它已有的 CLI `installation_id`。损坏的索引、损坏的身份文件或不一致的安装标识会报错，避免静默生成新身份。JSON 和凭据采用临时文件、fsync、原子替换；绑定写入失败时恢复内存状态。Unix 上凭据临时文件为 0600。

“导入本机当前凭据”及首次迁移只处理原生 `antigravity-oauth-token` / `jetski-standalone-oauth-token` 文件，不把通用 Gemini 的 `oauth_creds.json` 当作当前 agy 账号。若原账号仅存于系统凭据库，可先使用 agy-switch 导出完整账号凭据，结束旧会话后优先导入原账号，再添加其他账号；未能迁移且仍有旧会话时拒绝直接启用新账号，以免改变旧会话的账号归属。

这属于 CLI 配置和凭据的隔离，不是操作系统账户沙箱，不改变服务器 IP、系统硬件标识，也不保证上游无法关联同一服务器上的流量。

## 账号选择

客户端 API Key 的散列是账号绑定范围；无 Key 的请求共享匿名绑定。账号绑定独立于显式/自动会话身份；后者决定代理如何关联连续历史，而账号 host 只负责认证与上游客户端，不持有对话状态。因此回收 host、变更模型或上下文、取消请求、认证失败均不触发账号轮换。

首次选择按账号添加顺序取有凭据、未确认耗尽的账号。后续优先严格保留绑定。只有当前账号的相关额度耗尽才能从下一个账号继续查找，绑定立即原子保存，A 恢复后也不会把健康 B 的绑定抢回。

额度判定有两类证据：上游终止错误中明确的 quota exhausted / quota exceeded / 周期用量上限描述；以及近 65 秒的账号额度快照中明确的剩余 0。上游额度分组各自成为 scope：通用 Gemini、Claude/GPT，以及独立模型组（如 `Gemini 4 Argon` → `gemini-4-argon`）。独立组只阻断该组覆盖的模型，通用组会排除已有独立组的模型；因此 Argon 耗尽不会误伤同一账号的 Flash/Pro，通用 Gemini 耗尽也不影响仍有余额的 Argon。只有同一分组内的 weekly/5h 桶耗尽才阻断该组。独立模型组的剩余比例 ≤1% 也按不可用处理：上游可能把已耗尽的桶报成很小的正数（实测 Argon 为 0.55%），而通用组仍只认精确的 0，避免一个低子桶阻断整个厂商。未知默认模型的终止额度错误保守记为整个账号。未知数值、过期的快照/重置时间、通用组中低于某阈值但仍大于零的余额、一般 HTTP 429 或 RESOURCE_EXHAUSTED 不足以判定耗尽。频率、并发和容量限制不触发切换。旧版本写入的宽泛 `gemini` / `third-party` 记录仍按原语义生效；下一次额度快照会按实际分组替换或清除它们。

如果本轮还没有向适配器交付 delta/result，也没有产生无法撤销的客户端副作用，可以在另一个账号的 bridge host 上重放完整客户端历史。ready 事件只发一次；任何部分输出都会关闭本轮自动重试。旧账号的 host 不能被新账号复用，进程容量繁忙也不允许通过换号绕开限制。

没有上游重置时间时，不编造固定冷却期。用户在面板刷新额度后，只有较新的健康额度快照才清除相应耗尽记录；当前绑定仍保持不变。服务端所有账号耗尽时返回 429。

每个账号还可以限制可用模型：面板账号卡片的编辑弹窗按基础模型关闭或开启，保存为账号自己的 `disabled_models`。列表为空表示不限制；关闭某个基础模型会同时关闭其 low/medium/high 等全部思考档位。绑定账号请求被限制的模型返回 404 `model_disabled_for_account`，不会自动换号；尚未绑定账号的客户端会跳过限制该模型的账号。`/v1/models` 与 Gemini 模型列表按当前绑定账号过滤。

管理员确认删除账号是显式管理操作：关闭该账号的空闲 bridge host、清除其数据与绑定，后续请求重新选择可用账号；进行中的请求或登录会阻止删除。删除最后一个账号后仍保留隔离模式，不自动回退到旧的本机凭据。编辑账号可同时修改显示名称和可用模型；改名本身不改变身份、凭据或绑定。

## 验证范围

`core/tests/test_account.py` 覆盖账号私有目录、凭据和状态、稳定安装 ID、绑定跨回收/重启保持、额度失败后的完整历史重放、并发切换、无额度可用、普通错误不换号、额度查询、模型缓存分区、登录桥目标及旧账号迁移；`core/tests/test_account_pool.py` 覆盖独立额度 scope、通用组排除、旧宽泛记录迁移和额度恢复；`tests/test_app_factory.py`、`tests/test_boundaries.py` 和 `core/tests/test_bridge_*` 覆盖 core/full 装配、边界与 bridge 行为。真实 CLI 探针只调用模型列表并使用无效测试凭据，不执行真实模型生成。
