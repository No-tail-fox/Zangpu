# 管理员 API 使用说明

管理员 API 与外部 caller API 使用不同的认证边界。管理员入口只通过管理站端口提供，默认绑定 `127.0.0.1:9001`；`/api/v1/admin/*` 由 Caddy 转发到控制面 backend，其他路径回退到中文 Web。

## 部署配置

生产环境必须分别提供：

- `ADMIN_SESSION_SECRET`：至少 32 字节，仅用于签发 `zpa1` 管理员会话。
- `ADMIN_LOGIN_TOKEN`：至少 32 字节，仅用于首次登录校验，不作为会话 Cookie。
- `EVENT_RETENTION_DAYS`：终态调用事件保留天数，默认 180，范围 30 至 3650。
- `ADMIN_AUDIT_RETENTION_DAYS`：管理员审计保留天数，默认 730，范围 365 至 3650，且不得短于事件保留期。
- `RETENTION_BATCH_SIZE`：每次对事件表、审计表各自最多删除的行数，默认 1000，范围 1 至 10000。

两个值不得相同，不得写入 Web 构建、浏览器配置、日志或仓库。生产环境缺少 `ADMIN_LOGIN_TOKEN` 时 backend 拒绝启动。外层反向代理负责 TLS、来源网络和额外登录限流。

## 会话流程

1. `POST /api/v1/admin/session`，在 `X-Zangpu-Admin-Token` 请求头传入登录令牌。
2. 成功响应设置 `HttpOnly`、`SameSite=Strict` 的 `zangpu_admin_session` Cookie，并返回当前会话的 `csrf_token` 和过期时间。
3. 所有读取请求携带会话 Cookie；所有写请求还必须在 `X-Zangpu-CSRF` 请求头传入当前 `csrf_token`。
4. `POST /api/v1/admin/session/logout` 清除会话 Cookie。

会话默认有效期为 3600 秒，可用 `ZANGPU_ADMIN_SESSION_TTL_SECONDS` 在 300 至 86400 秒内调整。管理员会话不接受 caller 的 HMAC Key/Secret，caller API 也不接受管理员 Cookie。

中文管理站已接入上述会话和 caller 生命周期。登录令牌只放入 `X-Zangpu-Admin-Token` 请求头，不写入请求正文或浏览器存储；会话 Cookie 由浏览器以 HttpOnly 方式管理，CSRF Token 仅保留在当前页面内存。`401` 会清空页面状态并返回登录入口；网络登出失败时页面不会假装会话已经清除。

管理站支持调用方列表、创建、详情、绑定同步状态、权限/配额编辑、凭据轮换/撤销和调用方禁用。创建/轮换得到的 Secret 只在确认框显示一次，操作员明确确认已安全保存后才可关闭；关闭时页面立即清除 Secret 状态。管理站不读取积分余额，不直接调用 Open WebUI 或 Bifrost，也不把本地提交误报为远端同步完成。

## 接口清单

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/admin/session` | 登录并签发会话 |
| `GET` | `/api/v1/admin/session` | 读取当前会话和 CSRF Token |
| `POST` | `/api/v1/admin/session/logout` | 登出 |
| `GET` | `/api/v1/admin/callers` | 分页列出调用方 |
| `POST` | `/api/v1/admin/callers` | 创建调用方、初始凭据和 Bifrost 同步任务 |
| `GET` | `/api/v1/admin/callers/{client_id}` | 读取调用方、凭据摘要和绑定状态 |
| `PATCH` | `/api/v1/admin/callers/{client_id}` | 按版本更新权限和配额 |
| `POST` | `/api/v1/admin/callers/{client_id}/credentials/rotate` | 轮换凭据并撤销旧凭据 |
| `POST` | `/api/v1/admin/callers/{client_id}/credentials/{credential_id}/revoke` | 单独撤销凭据 |
| `POST` | `/api/v1/admin/callers/{client_id}/disable` | 本地禁用、撤销全部凭据并排队禁用 Bifrost Key |
| `GET` | `/api/v1/admin/events` | 筛选并分页读取不可变终态调用事件 |
| `GET` | `/api/v1/admin/events/summary` | 汇总请求、Token、计费、耗时百分位和 UTC 趋势 |
| `POST` | `/api/v1/admin/events/export` | 按当前筛选导出受限的安全 CSV，并写管理员审计 |
| `GET` | `/api/v1/admin/retention/preview` | 预览固定策略下的 cutoff、过期总数和下一批数量 |
| `POST` | `/api/v1/admin/retention/purge` | 按预览快照确认并清理一批过期事件/审计 |

创建与禁用请求必须携带 8 至 128 字符的 `Idempotency-Key`。更新请求必须携带当前 `expected_version`；版本过期返回 `409 ADMIN_CALLER_CONFLICT`，避免覆盖其他管理员刚完成的变更。

## 创建调用方

示例仅展示字段形状，不包含可用凭据：

```json
{
  "name": "业务系统 A",
  "description": "藏医药问答接入",
  "service_user_id": "<Open WebUI service user id>",
  "provider": "<Bifrost provider>",
  "model": "<initial model>",
  "allowed_endpoints": ["chat.completions", "models.read", "usage.read"],
  "allowed_models": ["<initial model>"],
  "group_ids": [],
  "qps_limit": 10,
  "concurrency_limit": 2,
  "daily_request_limit": 1000,
  "daily_token_limit": 1000000,
  "total_request_limit": null,
  "total_token_limit": null,
  "max_output_tokens_per_request": 4096
}
```

初始 `model` 必须包含在 `allowed_models` 中。当前 Bifrost binding 是每个 caller 一个初始 Provider/model；扩大 caller 模型权限不等于远端 Provider 已支持多个模型，部署人员必须同时核对 Bifrost 配置。

成功响应中的 caller `secret` 只显示一次。数据库仅保留 AES-GCM 密文、nonce、主密钥版本和指纹；列表、详情、审计和后续读取接口不会再次返回明文 Secret。

## 生效与审计

创建 caller 会在一个 SQL 事务中写入 caller、保护后的初始凭据、绑定、不可变管理员审计和 Bifrost outbox。HTTP `201` 表示本地期望状态已提交，不表示 Bifrost 已同步完成；通过详情中的 `binding.sync_status` 查看 `pending`、`active`、`error` 或 `disabled`。

禁用按“本地优先”执行：先禁用 caller、撤销全部活跃凭据并提交 outbox，再由部署调度的 `BifrostOutboxWorker.run_once()` 完成远端禁用。远端暂时失败不会重新启用 caller。

管理员审计只记录操作人、目标、动作、变更字段和脱敏前后摘要。它不记录登录令牌、会话 Cookie、CSRF Token、caller Secret、Bifrost Key、签名、nonce、请求正文或原始上游错误。

Open WebUI 仍是积分余额和流水的唯一权威；管理员 caller API 不读取或修改积分表。

## 调用事件、统计和导出

三个事件接口共用以下可选查询参数：

- `api_client_id`：调用方 ID。
- `created_from`、`created_to`：闭区间的 UTC Unix 秒；起始值不得晚于结束值。
- `outcome`、`stage`、`http_status`、`business_code`：终态结果、处理阶段、HTTP 状态码和稳定业务码。
- `endpoint`、`model_id`、`stream`：端点、模型和流式调用标记。

`GET /events` 还接受 `offset` 和 `limit`，其中单页最多 200 条。结果按 `created_at`、`id` 稳定倒序，并返回筛选后的 `total`。所有读取响应都带 `Cache-Control: no-store`。

`GET /events/summary` 的 `bucket_seconds` 只接受 `300`、`3600` 或 `86400`，按 UTC Unix 时间对齐趋势桶。汇总包括请求成功/失败数、Token、计费微单位、平均耗时、配额超限数以及 P50/P95/P99。百分位固定使用 nearest-rank：对排序后的 `n` 个耗时取 `ceil(p*n)`，并夹在首尾有效下标内。精确汇总最多处理 100,000 条事件；超出时返回 `422 ADMIN_OBSERVABILITY_LIMIT`，不抽样、不返回近似结果。

`POST /events/export` 属于写操作，除管理员 Cookie 外必须携带当前 `X-Zangpu-CSRF`。CSV 最多导出 10,000 条；超出时返回 `422 ADMIN_OBSERVABILITY_LIMIT`，要求缩小筛选范围。导出成功会写入不可变 `events.exported` 管理员审计。

CSV 仅包含终态事件的安全元数据。字符串中的 CR/LF/制表符会被替换；忽略前导空白后以 `=`、`+`、`-` 或 `@` 开头的值会加单引号，避免电子表格公式注入。导出不包含请求正文、prompt、answer、Secret、签名、nonce、原始 IP 或上游原始错误；`remote_ip_hash` 仍只是不可逆哈希。调用记录和聚合只读取控制面事件，不调用 Open WebUI、Bifrost，也不建立第二套积分余额。

## 保留期维护

保留期只由部署变量决定，管理员请求不能提交任意 cutoff。`GET /retention/preview` 按服务器当前 UTC Unix 秒计算两个开区间 cutoff：`created_at < now - retention_days * 86400` 的记录才算过期。响应同时返回过期总数、下一批数量和固定批次上限，并带 `Cache-Control: no-store`。

执行前必须把预览中的总数原样放入请求，并明确确认：

```json
{
  "expected_event_count": 1200,
  "expected_audit_count": 30,
  "confirmed": true
}
```

`POST /retention/purge` 还必须携带当前管理员 Cookie 和 `X-Zangpu-CSRF`。执行事务会重新计算两个总数；任一变化都返回 `409 ADMIN_RETENTION_SNAPSHOT_CHANGED` 并回滚。未确认返回 `422 ADMIN_RETENTION_CONFIRMATION_REQUIRED`；没有过期记录返回 `409 ADMIN_RETENTION_EMPTY`，且不写零行清理审计。

非空执行分别选择两个表中最老的 `created_at,id`，每表最多删除 `RETENTION_BATCH_SIZE` 行，验证实际删除行数后写入新的不可变 `retention.purged` 审计。响应返回本批删除数和剩余数；剩余不为零时重新预览后继续下一批，不能复用旧快照。

普通 ORM 对终态事件和管理员审计的更新/删除仍被拒绝。专用维护事务只删除过期 `ApiCallEvent` 和 `ApiClientAdminAudit`，不删除 `ApiCallOperation`，因此不会让请求 ID 幂等状态随事件保留期静默失效。FastAPI 生命周期不自动启动清理循环；生产环境由部署运维显式调度上述预览/确认流程。真实 PostgreSQL 锁、索引计划、vacuum 和备份恢复联动仍需在部署环境验收。
