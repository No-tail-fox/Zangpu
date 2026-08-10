# 管理员 API 使用说明

管理员 API 与外部 caller API 使用不同的认证边界。管理员入口只通过管理站端口提供，默认绑定 `127.0.0.1:9001`；`/api/v1/admin/*` 由 Caddy 转发到控制面 backend，其他路径回退到中文 Web。

## 部署配置

生产环境必须分别提供：

- `ADMIN_SESSION_SECRET`：至少 32 字节，仅用于签发 `zpa1` 管理员会话。
- `ADMIN_LOGIN_TOKEN`：至少 32 字节，仅用于首次登录校验，不作为会话 Cookie。

两个值不得相同，不得写入 Web 构建、浏览器配置、日志或仓库。生产环境缺少 `ADMIN_LOGIN_TOKEN` 时 backend 拒绝启动。外层反向代理负责 TLS、来源网络和额外登录限流。

## 会话流程

1. `POST /api/v1/admin/session`，在 `X-Zangpu-Admin-Token` 请求头传入登录令牌。
2. 成功响应设置 `HttpOnly`、`SameSite=Strict` 的 `zangpu_admin_session` Cookie，并返回当前会话的 `csrf_token` 和过期时间。
3. 所有读取请求携带会话 Cookie；所有写请求还必须在 `X-Zangpu-CSRF` 请求头传入当前 `csrf_token`。
4. `POST /api/v1/admin/session/logout` 清除会话 Cookie。

会话默认有效期为 3600 秒，可用 `ZANGPU_ADMIN_SESSION_TTL_SECONDS` 在 300 至 86400 秒内调整。管理员会话不接受 caller 的 HMAC Key/Secret，caller API 也不接受管理员 Cookie。

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
