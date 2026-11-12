# agy-proxy

本项目把下游客户端的请求转换成 agy CLI 内部模型请求；代理负责组装请求并转发响应，agy 负责认证与上游发包。

## 语言

**Donor**:
宿主不会调用的现有二进制函数；复用其代码槽与函数元数据承载新代码，原业务语义无关。
_Avoid_: 模型函数、获取模型函数、hook target

**H1 宿主**:
正常 agy CLI 进程 + 跳过 UI 入口的宿主形态；不依赖 stdin、PTY、TUI、print、stream-json、ACP 或 Hub。
_Avoid_: Hub 模式、headless 模式

**Bridge**:
代理与 agy 内部生成客户端之间的进程内转发层；代理组装 protojson，bridge 做 typed 转换、调用内部 client 并转发响应，agy 发包。
_Avoid_: upstream proxy；不要用 transport 指代 bridge（transport 已废弃，见 Protocol adapter）

**Endpoint discovery**:
代理定位 bridge 本地监听端点的方式；不得靠猜端口。
_Avoid_: port guessing

**Fail-closed**:
校验失败时拒绝生成或启动，不回退或猜测。
_Avoid_: best-effort fallback

**Session**:
下游一次有身份边界的对话；上下文由完整消息历史决定，不由 `conversation_id` 决定。
_Avoid_: 把 conversation_id 当 session key

## 架构分层

**Core**:
`agy-proxy-core` 发行版；拥有账号生命周期、路由、环境隔离、机器标识（`installation_id`）和 Gemini 协议服务，不包含下游协议适配层。
_Avoid_: 把 core 当作 bridge、CLI 或 dashboard 的同义词

**Protocol adapter**:
`agy-proxy-adapters` 发行版中的下游协议转换层；把 OpenAI Chat Completions、Responses、Anthropic Messages 与 Core 的 Gemini 契约互转，不接触账号、Bridge 或上游凭据。仓库目录 `adapters/`，import 包 `agy_proxy_adapters`。
_Avoid_: transport、gateway、wrapper

**Gemini contract**:
Core 对 Protocol adapter 与外部 Gemini 客户端暴露的 `GenerateContentRequest` / `GenerateContentResponse` 形状；本次拆分的规范中间格式。
_Avoid_: internal chat format、bridge request

**conversation_id**:
`GetChatMessageRequest` 的字段；不是 `sessionId`，不决定上下文；只在 `trajectory_id` 非空时进入 `requestId`。
_Avoid_: sessionId、session key

**sessionId**:
agy 对内部 conversation seed 做的签名 FNV-1a64；在 bridge 的 `GetChatMessage` 路径中不由 `conversation_id` 派生。
_Avoid_: conversation_id

## Bridge IPC

**Bridge request**:
代理发给 bridge 的 `api_server_pb.GetChatMessageRequest` protojson；Python 不生成 protobuf，bridge 做 typed 转换与响应转发。
_Avoid_: raw protobuf request、Python protobuf codegen

**Bridge event**:
`/_bridge` NDJSON 流中的一行；`response` 带 `raw_b64` 与 `json`，控制事件为 accepted/heartbeat/completed/failed/cancelled。
_Avoid_: SSE chunk、response_chunk

**Session affinity**:
会话固定绑定账号；并发限制只排队/背压，不换号。
_Avoid_: per-request load balancing、account reroute

**Account failover**:
仅在 auth/quota 或显式配置的 rate limit 下迁移账号，并重建完整历史、失效旧账号缓存/签名。
_Avoid_: silent reroute、concurrency failover

**Heartbeat**:
Bridge 等待上游时每 10 秒发送的无负载控制事件；只证明 bridge 存活，不重置总 deadline。
_Avoid_: keepalive response、message
