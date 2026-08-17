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
