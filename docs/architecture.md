# Control Plane Architecture

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

## Persistence Foundation

Task 1 establishes these standalone PostgreSQL truths without adding public management endpoints:

| Table | Responsibility |
| --- | --- |
| `api_client` | Caller policy, permissions and quota ceilings |
| `api_client_admin_audit` | Sanitized append-only administrator mutation and export history |
| `api_client_credential` | Public Key IDs and encrypted external HMAC material |
| `api_client_quota_usage` | Durable daily and lifetime counters |
| `api_call_operation` | Recoverable request state and settlement links |
| `api_call_event` | One append-only terminal evidence row per operation |
| `api_client_binding` | One service-user/Bifrost binding per caller |
| `control_outbox` | Transactional desired remote-state delivery |

All history-bearing foreign keys use restrictive deletion and have covering indexes. Admin audits contain only changed field names and bounded redacted before/after summaries; audit rows reject Secret, ciphertext, Authorization, signature, nonce, request content and raw upstream error fields. Pending/error bindings may exist before remote IDs are assigned; active/disabled bindings require complete remote identifiers and encrypted Bifrost material. Outbox payloads contain desired configuration only, never Secret, authorization or ciphertext fields.

The initial Alembic revision has one head and is verified by clean SQLite upgrade, metadata drift check, downgrade and re-upgrade. PostgreSQL SQL compilation is a source gate; execution against the deployment PostgreSQL remains an integration gate.

## Secret Handling

Required Secret settings use redacted Pydantic types. Caller Secrets and Bifrost virtual keys are not browser configuration, defaults, health fields or ordinary log context. Compose requires deployment-provided values and does not ship working Secret defaults.

ORM entities retain ciphertext for later security services, while default credential and binding response projections omit ciphertext, nonce, fingerprint and key-version fields. Terminal call events reject ORM entity and bulk mutations; retention deletion must use its future explicit bounded maintenance path.
