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

## Database Migrations

Alembic fails closed unless `sqlalchemy.url` or `ZANGPU_DATABASE_URL` is supplied. Apply the standalone control-plane schema with:

```powershell
$env:ZANGPU_DATABASE_URL='postgresql+psycopg://<user>:<password>@<host>:5432/<database>'
.\.bootstrap-uv\Scripts\uv.exe run alembic upgrade head
```

## Credential Key Ring

`ZANGPU_API_CREDENTIAL_KEYS` is a JSON object that maps bounded version IDs to base64-encoded 32-byte AES keys. `ZANGPU_API_CREDENTIAL_ACTIVE_KEY_ID` must select one version in that object. Both values come from the deployment environment or Secret Manager; invalid JSON, key lengths or active versions fail application startup.

`ZANGPU_BIFROST_MANAGEMENT_TOKEN` is an independent scoped management credential and `ZANGPU_BIFROST_EXPECTED_VERSION` must match the pinned runtime (currently `v1.6.3`). Startup calls the private health, version, config, Provider, model and virtual-key routes. It fails closed on an unexpected version, SPA HTML fallback, missing Provider/model, disabled inference authentication, enabled direct keys or any raw request/response storage/return flag.

Distributed-control defaults are a 300-second timestamp tolerance, 600-second nonce TTL, 60-second concurrency lease and 15-second heartbeat. The corresponding `ZANGPU_CONTRACT_API_*` settings are bounded; nonce TTL must be at least twice the timestamp tolerance and heartbeat must stay below half the lease.

Task 0 supplies the service boundary, Task 1 persistence truth, Task 2 HMAC/protected credentials, Task 3 atomic Redis nonce/QPS/concurrency primitives and Task 4 the private typed Bifrost client plus binding reconciliation. Public inference lifecycle, SQL quota admission, credit settlement and load acceptance remain later scoped tasks.
