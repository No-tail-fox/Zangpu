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

Task 0 supplies the service boundary, Task 1 supplies persistence truth and Task 2 supplies HMAC v1 canonicalization plus protected caller credentials. Redis nonce replay protection, distributed limits, quota admission, Bifrost proxying, credit settlement and load acceptance remain later scoped tasks.
