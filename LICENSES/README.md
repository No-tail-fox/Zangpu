# License Boundary

First-party control-plane source is project-owned. No redistribution license is implied until a root license is approved by the project owner.

The current scaffold selects dependencies with commercially usable upstream terms:

| Component | Upstream license | Role |
| --- | --- | --- |
| FastAPI, Pydantic, Uvicorn | MIT / BSD-3-Clause | Backend runtime |
| SQLAlchemy, Alembic | MIT | Database models and migrations |
| Psycopg, psycopg-binary | LGPL-3.0-only | PostgreSQL driver |
| cryptography 44.0.0 | Apache-2.0 OR BSD-3-Clause | AES-256-GCM caller credential protection |
| redis-py | MIT | Async Redis-compatible client |
| httpx | BSD-3-Clause | Python SDK runtime |
| hatchling | MIT | Python SDK build-only backend |
| Node.js 20+ built-in APIs | MIT | External JavaScript SDK runtime; not bundled by this repository |
| fakeredis / lupa / sortedcontainers | BSD / MIT-style / Apache-2.0 | Development-only atomic Lua verification |
| Svelte, SvelteKit, Lucide | MIT / ISC | Operator site |
| PostgreSQL | PostgreSQL License | Durable control-plane truth |
| Valkey | BSD-3-Clause | Redis-compatible nonce/QPS/concurrency store |
| Caddy | Apache-2.0 | Reverse proxy |
| Bifrost OSS v1.6.3 | Apache-2.0 | Internal model gateway |
| Grafana k6 CLI | AGPL-3.0-only | External development/load-test tool; not bundled in product artifacts |

Deployment must provide a license-verified, version-pinned Bifrost image reference. Do not replace it with an enterprise/latest tag. Container digests, corresponding-source/license obligations and complete third-party notices are release-gate artifacts, not inferred from this bootstrap file.
