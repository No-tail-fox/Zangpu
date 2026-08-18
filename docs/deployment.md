# 控制面部署与 Smoke

## 证据边界

本页给出控制面源码的部署准备、迁移和低成本 smoke 命令。`docker compose config`、本地 loopback 或 SDK smoke 不能替代目标环境中的 PostgreSQL、Valkey、Open WebUI、Bifrost、GPU/模型和备份恢复验收。

Compose 只把 caller 端口 9000 和管理员端口 9001 绑定到 `127.0.0.1`。PostgreSQL、Valkey、Bifrost、backend 和 Web 不发布宿主机端口。生产入口应在外层 TLS 反向代理或受控内网之后。

## 必需配置

部署前通过 Secret 管理器或受控环境提供：

- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `BIFROST_IMAGE`，必须是完成许可证核验的固定镜像 digest
- `BIFROST_MANAGEMENT_TOKEN` 与 `BIFROST_EXPECTED_VERSION`
- `OPENWEBUI_INTERNAL_BASE_URL`、`OPENWEBUI_INTERNAL_SERVICE_ID`、`OPENWEBUI_INTERNAL_SERVICE_SECRET`
- `ADMIN_SESSION_SECRET`、`ADMIN_LOGIN_TOKEN`
- `API_CREDENTIAL_KEYS`、`API_CREDENTIAL_ACTIVE_KEY_ID`

不要把真实值写入仓库、Compose 文件、浏览器配置、日志或 smoke 结果。

同时必须提供模型池和排队策略：

- `MODEL_POOL_POLICIES` 是按模型 ID 索引的 JSON 对象。每个值必须包含
  `pool_id` 和 `active_limit`，例如：

  ```json
  {"qwen2.5-7b-instruct":{"pool_id":"qwen2.5-7b","active_limit":8}}
  ```

  `active_limit` 是经过目标 GPU、模型、量化、上下文长度和 Token 分布实测后的
  同时生成上限；它保护模型服务，不等于 200 个在线用户，也不能在没有 GPU 证据时直接设置为 200。
  同一模型池中的模型必须使用相同的 `active_limit`。
- `CONTRACT_API_GLOBAL_QUEUE_LIMIT` 默认 `200`，是所有模型池共享的等待 ticket 上限。
- `CONTRACT_API_CALLER_QUEUE_LIMIT` 默认 `8`，限制单个调用方的等待 ticket 数量。
- `CONTRACT_API_QUEUE_WAIT_SECONDS` 默认 `30`，ticket 的绝对等待期限；超时不会继续占用队列。
- `CONTRACT_API_QUEUE_POLL_MILLISECONDS` 默认 `250`，后台轮询队列头的间隔。

模型池准入在调用方并发、SQL 配额、Open WebUI 积分和 Bifrost 推理之前执行。队列满、调用方队列满、等待超时或 Valkey 不可用都会快速失败，返回稳定的容量错误和 `Retry-After`/容量响应头，不扣费、不预留配额，也不调用上游模型。

这些设置只冻结硬件无关的准入语义。当前工作站没有 Docker，且尚未提供目标 GPU/模型参数，因此不能据此声称真实 PostgreSQL、Valkey、Bifrost、Open WebUI 联调或 200 路活跃生成已经通过。真实验收仍需按 `20 -> 50 -> 100 -> 200` 分阶段运行连接、排队和 GPU 推理压测。

## 四副本软件路径

Compose 显式运行 `backend-1` 到 `backend-4` 四个 Control Plane 容器，每个容器保持一个 Uvicorn worker。它们使用相同的受控配置，共享 PostgreSQL、Valkey、单个 Bifrost OSS 和外部 Open WebUI。HMAC 调用认证与管理员签名会话都是无服务端会话状态的，Caddy 不需要粘性路由；全局 nonce、QPS、调用方并发、模型池 active lease 和等待 ticket 继续由共享 Valkey 决定。

Caddy 对四个 backend 使用 `least_conn`，每 10 秒检查一次 public health。chat 流返回 `Content-Type: text/event-stream`，Caddy 会按内置 SSE 路径立即刷新且未配置 `response_buffers`；这里刻意不使用 `flush_interval -1`，因为该低延迟模式会在客户端提前断开后继续保持 backend 请求。配置没有开启负载均衡重试；一个已发送的 chat 请求不会因为切换上游而被网关静默重复提交。

在有 Docker 的目标服务器上，先完成配置展开，再启动并检查全部副本：

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml config
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
```

`gateway`、`backend-1..4`、`web`、`postgres` 和 `redis` 必须为 healthy/ready，Bifrost 必须完成版本与安全 preflight。四个 backend 会分别建立数据库和上游 HTTP 连接池；验收时必须记录 PostgreSQL 连接峰值，超过单实例预算前引入 PgBouncer 或托管 PostgreSQL。这个四副本剖面提高 HTTP/SSE 软件路径容量，但不增加 GPU 推理槽位，也不消除单 Bifrost、单 PostgreSQL 和单 Valkey 的单服务器故障域。

## 结构检查与迁移

```powershell
docker compose -f deploy/compose.yaml config
$env:ZANGPU_DATABASE_URL='postgresql+psycopg://<user>:<password>@<host>:5432/<database>'
.\.bootstrap-uv\Scripts\uv.exe run alembic upgrade head
.\.bootstrap-uv\Scripts\uv.exe run alembic current
```

当前迁移唯一 head 必须为 `0004`。首次部署、升级和回滚演练都应保存命令时间、版本提交、数据库备份标识和脱敏结果。

## 低成本 API Smoke

管理员创建 caller 后，从一次性响应把 Key ID 与 Secret 写入进程环境或临时 Secret 注入，不写入脚本：

```powershell
$env:ZANGPU_API_BASE_URL='https://api.example.com'
$env:ZANGPU_API_KEY_ID='<issued-key-id>'
$env:ZANGPU_API_SECRET='<load-from-secret-manager>'
node .\examples\javascript\deploy-smoke.mjs
```

脚本依次验证 public health、调用方可见模型和当前调用方用量，只输出服务版本、模型数量与用量时间点。它不调用 chat、不触发模型推理、不读取 Open WebUI 积分余额，也不输出 Secret、签名、nonce 或请求正文。

聊天 smoke 必须另外明确费用确认并选择 `/models` 返回的模型：

```powershell
$env:ZANGPU_CONFIRM_CHAT_SPEND='YES'
$env:ZANGPU_CHAT_MODEL='<allowed-model-id>'
node .\examples\javascript\chat.mjs
```

真实部署验收还必须覆盖迁移、outbox、Open WebUI 结算/退款、流式中断恢复、k6 smoke/steady/burst、retention 调度、vacuum、备份恢复和故障回滚。
