# Zangpu API Control Plane

Independent contract API management service and Chinese operator site for Zangpu. Bifrost remains an internal model gateway, while Open WebUI remains the only credit-balance authority.

## Local Gates

```powershell
.\.bootstrap-uv\Scripts\uv.exe run pytest backend/tests/test_bootstrap.py -q
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

Task 0 is a bootstrap boundary only. HMAC, replay protection, distributed limits, exact quota, Bifrost proxying, credit settlement and load acceptance are implemented in later scoped tasks.
