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

## Caller Authentication

Task 2 implements the frozen HMAC v1 canonical request over method, normalized RFC 3986 path/query, raw-body SHA-256, public Key ID, timestamp, nonce and request ID. Raw ASGI headers are parsed directly so signed duplicates cannot be collapsed. Known credential signatures use constant-time comparison; malformed headers, unknown/revoked/expired credentials, clock skew, decryption failures and bad signatures expose one `401 AUTH_FAILED` response.

Caller Secrets are generated with high entropy and can be consumed once by the future creation route. Only AES-256-GCM ciphertext, a 96-bit nonce, key version and SHA-256 fingerprint enter PostgreSQL. Encryption AAD binds credential ID, caller ID, public Key ID and master-key version; unavailable versions, modified AAD and tampering fail closed. A valid caller signature is required before a disabled caller receives the stable `403 CLIENT_DISABLED` response.

## Distributed Admission

Task 3 adds fail-closed Redis-compatible controls without any process-local fallback. Nonce claim uses one `SET NX EX`; QPS uses a single-key Lua sliding window with Redis `TIME`; concurrency acquire, heartbeat and exact-owner release use atomic sorted-set scripts scored by server-side lease expiry. Caller, credential, nonce, request and operation identifiers are SHA-256 encoded before they enter Redis keys or members.

The frozen defaults are a 600-second nonce TTL, one-second QPS window and 60-second concurrency lease. Heartbeat scheduling remains 15 seconds. Redis timeout, connection failure or malformed script response maps to stable `503 CONTROL_PLANE_UNAVAILABLE`; it never admits external inference through an in-memory limiter.

Concurrent Lua behavior and TTL recovery pass under `fakeredis[lua]`. Docker is unavailable on this workstation, so the same race/TTL suite against the pinned deployment Valkey remains an integration and acceptance gate.

## Bifrost Binding Reconciliation

Task 4 adds a private typed client for the pinned Bifrost management and OpenAI-compatible inference routes. Management requests carry only the scoped Bifrost management token; inference requests carry only the encrypted binding's `x-bf-vk` value, so the two credentials cannot cross request surfaces. JSON response content type and bounded payload parsing are mandatory; an HTTP 200 SPA document is a protocol failure. Network/protocol failures expose stable codes without raw bodies or sensitive HTTP exception chains.

Virtual-key creation material is held by a one-time redacted transport object and immediately stored as an AES-GCM envelope. Its AAD binds the local binding ID, caller ID, Bifrost virtual-key ID and master-key version. The raw value never enters response projections or outbox payloads. A combined binding becomes `active` or `disabled` only after both the Open WebUI service-user ID and protected Bifrost key fields exist; otherwise it remains `pending`.

Outbox workers claim bounded batches with `FOR UPDATE SKIP LOCKED`, commit the claim, perform remote I/O without an open SQL transaction and finalize in a new transaction. Create retries reconcile by stable managed key name. Disable happens locally first, treats a missing remote key as already disabled, retries transient failures with bounded exponential backoff and stops retrying stable authentication/request errors. PostgreSQL/Bifrost execution of this worker remains a deployment integration gate; SQLite plus the live loopback Bifrost v1.6.3 PoC cover the local source contract.

## Secret Handling

Required Secret settings use redacted Pydantic types. The caller credential key ring is a bounded JSON version map supplied only by the environment or Secret Manager, and application startup validates its 32-byte keys and active version. Caller Secrets and Bifrost virtual keys are not browser configuration, defaults, health fields or ordinary log context. Compose requires deployment-provided values and does not ship working Secret defaults.

ORM entities retain ciphertext for later security services, while default credential and binding response projections omit ciphertext, nonce, fingerprint and key-version fields. Terminal call events reject ORM entity and bulk mutations; retention deletion must use its future explicit bounded maintenance path.
