# Zangpu API Control Plane

Independent contract API management service and Chinese operator site for Zangpu. Bifrost remains an internal model gateway, while Open WebUI remains the only credit-balance authority.

## Local Gates

```powershell
.\.bootstrap-uv\Scripts\uv.exe run pytest backend/tests -q
.\.bootstrap-uv\Scripts\uv.exe run ruff check backend
pnpm --dir web test
docker compose -f deploy/compose.yaml config
```

The Docker command requires deployment Secrets and a license-verified pinned `BIFROST_IMAGE`. Docker is not required for the source tests, but compose runtime verification remains incomplete until a Docker environment is available.

## Development

Create a local backend environment file outside version control with all `ZANGPU_` settings, then run:

```powershell
.\.bootstrap-uv\Scripts\uv.exe run uvicorn backend.app.main:app --host 127.0.0.1 --port 9000
pnpm --dir web dev
```

## API SDK/cURL

The installable Python SDK, executable PowerShell `curl.exe` signer and Chinese integration guidance are documented in [`docs/api-sdk.md`](docs/api-sdk.md). Examples cover caller-scoped models, usage, JSON chat and SSE chat without embedding credentials or automatically retrying inference.

## Administrator API

The independent administrator session, caller/credential lifecycle, permission/quota updates, observability/export, bounded retention maintenance and Bifrost outbox semantics are documented in [`docs/admin-api.md`](docs/admin-api.md). Production requires separate `ADMIN_SESSION_SECRET` and `ADMIN_LOGIN_TOKEN` values. Caller Secrets are returned once and never appear in list/detail responses.

## Load Acceptance

The dependency-free signed k6 script, PowerShell runner, configurable smoke/steady/burst profiles and Chinese result workflow are documented in [`docs/load-testing.md`](docs/load-testing.md). Metadata load is the safe default; chat load requires explicit credit-spend confirmation. Generated summaries are aggregate-only and belong under the ignored `.tmp/k6-results` directory.

## Database Migrations

Alembic fails closed unless `sqlalchemy.url` or `ZANGPU_DATABASE_URL` is supplied. Apply the standalone control-plane schema with:

```powershell
$env:ZANGPU_DATABASE_URL='postgresql+psycopg://<user>:<password>@<host>:5432/<database>'
.\.bootstrap-uv\Scripts\uv.exe run alembic upgrade head
```

Retention defaults are 180 days for terminal events, 730 days for administrator audits and 1,000 rows per table per purge. `ZANGPU_EVENT_RETENTION_DAYS`, `ZANGPU_ADMIN_AUDIT_RETENTION_DAYS` and `ZANGPU_RETENTION_BATCH_SIZE` are bounded deployment settings; administrator audit retention cannot be shorter than event retention. The control plane does not run an implicit lifespan cleanup loop. Deployment operations must schedule the authenticated preview/confirm flow documented in `docs/admin-api.md` and retain its audit evidence.

## Credential Key Ring

`ZANGPU_API_CREDENTIAL_KEYS` is a JSON object that maps bounded version IDs to base64-encoded 32-byte AES keys. `ZANGPU_API_CREDENTIAL_ACTIVE_KEY_ID` must select one version in that object. Both values come from the deployment environment or Secret Manager; invalid JSON, key lengths or active versions fail application startup.

`ZANGPU_BIFROST_MANAGEMENT_TOKEN` is an independent scoped management credential and `ZANGPU_BIFROST_EXPECTED_VERSION` must match the pinned runtime (currently `v1.6.3`). Startup calls the private health, version, config, Provider, model and virtual-key routes. It fails closed on an unexpected version, SPA HTML fallback, missing Provider/model, disabled inference authentication, enabled direct keys or any raw request/response storage/return flag.

Distributed-control defaults are a 300-second timestamp tolerance, 600-second nonce TTL, 60-second concurrency lease and 15-second heartbeat. The corresponding `ZANGPU_CONTRACT_API_*` settings are bounded; nonce TTL must be at least twice the timestamp tolerance and heartbeat must stay below half the lease.

`POST /api/v1/external/chat/completions` is the signed caller route for both bounded JSON and SSE responses. It enforces a 1 MiB body cap and the smaller of the caller output limit and `ZANGPU_CONTRACT_API_MAX_OUTPUT_TOKENS` (default `4096`). Stream responses force private usage evidence, send heartbeats while waiting or receiving a long response, and do not forward `[DONE]` until credit settlement and the SQL terminal event commit. All responses carry a server `X-Zangpu-Request-Id`; successful responses also include QPS limit headers.

`GET /api/v1/external/models` and `GET /api/v1/external/usage` use the same empty-body HMAC contract, nonce protection and caller QPS window. They require `models.read` and `usage.read` respectively. Models are limited to the caller's configured allow-list; usage reports only the current caller's UTC-daily and lifetime SQL quota counters and limits. The usage response does not expose or duplicate the Open WebUI credit balance. Query parameters and GET bodies are rejected.

Tasks 0-10 now cover the service boundary, persistence, HMAC credentials, Redis admission, private Bifrost binding, Open WebUI credit integration, durable JSON/SSE chat lifecycles, signed caller metadata, SDK/cURL delivery and k6 load-acceptance tooling. A bounded `ExternalChatRecoveryWorker.run_once()` must be scheduled by deployment operations; it reconciles stale pending operations through Open WebUI status/settle/cancel only and never replays Bifrost. Real PostgreSQL, Valkey, Open WebUI and Bifrost execution, including the final performance report, remains a deployment gate because Docker is unavailable on this workstation.
