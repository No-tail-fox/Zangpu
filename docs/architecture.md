# Task 0 Architecture

## Boundary

The standalone control plane owns external callers, credentials, contract quotas and immutable operation evidence. It does not live inside Open WebUI and never writes Open WebUI credit tables directly.

Only the reverse proxy publishes host ports:

- `127.0.0.1:9000` for the caller API.
- `127.0.0.1:9001` for the Chinese operator site.

FastAPI, SvelteKit, Bifrost, PostgreSQL and Redis-compatible Valkey use compose-internal DNS and expose no host ports. Production TLS and network policy belong at the deployment reverse proxy.

## Truth Ownership

| Concern | Authority |
| --- | --- |
| Caller identity and credential state | Control-plane PostgreSQL |
| Nonce, QPS and concurrency | Redis-compatible Valkey |
| Daily and lifetime quota | Control-plane PostgreSQL |
| Provider/model forwarding | Internal Bifrost |
| User/service-account balance | Open WebUI credit ledger |

## Bootstrap Scope

Task 0 provides fail-closed settings, a versioned health route, a Chinese operator shell and a private compose topology. It does not yet implement caller credentials, HMAC, quota admission, inference proxying, credit settlement or acceptance load tests.

## Secret Handling

Required Secret settings use redacted Pydantic types. Caller Secrets and Bifrost virtual keys are not browser configuration, defaults, health fields or ordinary log context. Compose requires deployment-provided values and does not ship working Secret defaults.
