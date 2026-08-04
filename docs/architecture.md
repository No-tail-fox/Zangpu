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

The Alembic chain has one head and is verified by clean SQLite upgrade, metadata drift check, downgrade and re-upgrade. Revision `0002` adds immutable `quota_overrun` evidence while preserving existing event rows. PostgreSQL SQL compilation is a source gate; execution against the deployment PostgreSQL remains an integration gate.

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

## Open WebUI Credit Lifecycle

Task 5 keeps Open WebUI as the only balance authority. The control plane calls six hidden internal routes to resolve a deterministic non-login service user and reserve, settle, cancel, refund or inspect one UUID-addressed operation. It never writes Open WebUI credit tables directly. Reservation and terminal retries reuse the same operation identity; refund accepts no caller-supplied amount and is one idempotent full correction derived from the charged settlement.

Every request is compact sorted JSON signed with HMAC-SHA256 over the protocol version, service ID, timestamp, uppercase method, raw target and body digest. Only the dedicated internal service ID, timestamp and signature cross this boundary; caller Secrets, nonces, Authorization headers and Bifrost keys do not. Open WebUI independently enforces the matching service identity, Secret, clock skew and direct source CIDR allow-list.

The application owns one bounded typed Open WebUI client and closes it during lifespan shutdown. Startup creates the client but sends no mutating service-user request or speculative preflight. Open WebUI remains an externally supplied private-network origin rather than a Compose-owned service, so real PostgreSQL cross-service execution remains a deployment integration gate.

## External Chat Lifecycle

Tasks 6-7 expose only `POST /api/v1/external/chat/completions` and the bounded OpenAI-compatible request subset. The raw JSON body is capped at 1 MiB, output Tokens are bounded by both caller policy and `ZANGPU_CONTRACT_API_MAX_OUTPUT_TOKENS`, and caller-supplied tools, files, direct Provider controls and unknown fields are rejected rather than forwarded. `stream=true` returns a no-buffer SSE response; `stream=false` retains the JSON response contract.

Admission order is fixed: raw request bounds, HMAC authentication, nonce claim, endpoint/model policy, QPS, concurrency lease, SQL request/Token reservation, Open WebUI credit reservation, Bifrost inference, credit settlement, SQL terminal event, then exact-owner concurrency release. Credential/client state is re-read under the SQL reservation transaction so a post-signature revoke or expiry cannot race through quota admission.

The signed caller request ID is the idempotency key. A matching pending operation returns `REQUEST_IN_PROGRESS`; a matching terminal operation returns `REQUEST_ALREADY_COMPLETED`; a different fingerprint returns `REQUEST_ID_CONFLICT`. The response body is not stored or replayed, and none of these cases can invoke Bifrost or credit admission a second time. Provider failures cancel credit before releasing unused Token reservation. If Provider usage exists but credit settlement is uncertain, the operation remains pending with protected settlement and usage evidence for a later recovery task rather than guessing or double charging.

For streaming, the private Bifrost payload forces `stream_options.include_usage=true`. An incremental decoder validates complete SSE frames and requires one total-matching usage snapshot followed by `[DONE]`. The concurrency lease is renewed on an absolute heartbeat schedule even when chunks are continuous, and the same heartbeat refreshes the pending operation timestamp. Credit settlement and the SQL terminal event commit before `[DONE]` is forwarded. A disconnect or provider/protocol failure settles only known usage; otherwise it cancels without charge. Errors after HTTP headers use a sanitized SSE error frame, while admission failures remain JSON errors.

Every public response uses the bounded contract error envelope and a server-generated `X-Zangpu-Request-Id`; successful responses also carry the applicable QPS limit, remaining and reset headers. Events contain stable codes, measured milliseconds, actual Token/charge counters, stream mode and `quota_overrun`, never prompts, answers, Secrets, signatures, nonces, virtual keys or raw upstream errors. `ExternalChatRecoveryWorker.run_once()` is a bounded deployment-scheduled reconciliation pass: it uses Open WebUI operation status plus exact local usage evidence to settle or cancel stale pending operations, never calls Bifrost and never estimates usage. SDK/cURL examples and k6 acceptance remain separate follow-on tasks.

## External Caller Metadata

Task 8 adds signed `GET /api/v1/external/models` and `GET /api/v1/external/usage`. Both authenticate the exact empty-body request target, reject query parameters and GET bodies, claim the nonce, re-read caller/credential state and consume the caller's shared QPS window. They require the existing `models.read` and `usage.read` endpoint permissions.

The models route projects only `ApiClient.allowed_models`; it never lists Bifrost's global catalogue or decrypts a virtual key. The usage route projects only the matching caller's current UTC-daily and lifetime `ApiClientQuotaUsage` rows plus the caller's configured ceilings. Token remaining subtracts both consumed and in-flight reserved Tokens. A missing current-period row renders as zero without creating one.

Metadata requests do not acquire concurrency, reserve request/Token quota, create operations/events, call Bifrost or access Open WebUI. In particular, the usage response contains no credit balance because Open WebUI remains the sole balance authority. SDK/cURL examples and k6 acceptance remain separate follow-on tasks.

## Secret Handling

Required Secret settings use redacted Pydantic types. The caller credential key ring is a bounded JSON version map supplied only by the environment or Secret Manager, and application startup validates its 32-byte keys and active version. Caller Secrets and Bifrost virtual keys are not browser configuration, defaults, health fields or ordinary log context. Compose requires deployment-provided values and does not ship working Secret defaults.

ORM entities retain ciphertext for later security services, while default credential and binding response projections omit ciphertext, nonce, fingerprint and key-version fields. Terminal call events reject ORM entity and bulk mutations; retention deletion must use its future explicit bounded maintenance path.
