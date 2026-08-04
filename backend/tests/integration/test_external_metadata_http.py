import base64
import json
import time
from dataclasses import dataclass

from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.external import router
from backend.app.limits.nonce import NonceGuard
from backend.app.limits.qps import SlidingWindowQps
from backend.app.models import Base
from backend.app.models.clients import ApiClient
from backend.app.models.credentials import ApiClientCredential
from backend.app.models.events import ApiCallEvent
from backend.app.models.operations import ApiCallOperation
from backend.app.models.quotas import ApiClientQuotaUsage
from backend.app.security.canonical import body_sha256_hex, create_canonical_request, sign_canonical_request
from backend.app.security.credentials import create_protected_credential
from backend.app.security.dependencies import ExternalAuthenticator
from backend.app.security.keyring import CredentialKeyring
from backend.app.services.callers import DatabaseCredentialResolver
from backend.app.services.metadata import ExternalMetadataService
from backend.app.services.quota import utc_day_start


@dataclass(frozen=True, repr=False)
class MetadataHttpRuntime:
    app: FastAPI
    sessions: sessionmaker[Session]
    redis: FakeRedis
    secret: str
    now: int


def build_runtime() -> MetadataHttpRuntime:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = int(time.time())
    encoded = base64.b64encode(bytes(range(32))).decode("ascii")
    ring = CredentialKeyring.from_json(
        SecretStr(json.dumps({"v1": encoded})),
        active_key_id="v1",
    )
    client = ApiClient(
        id="client-http-1",
        name="Metadata HTTP Caller",
        description=None,
        status="active",
        allowed_endpoints=["models.read", "usage.read"],
        allowed_models=["model-2", "model-1"],
        group_ids=[],
        qps_limit=20,
        concurrency_limit=2,
        daily_request_limit=10,
        daily_token_limit=100,
        total_request_limit=100,
        total_token_limit=1_000,
        max_output_tokens_per_request=128,
        version=1,
        created_by="admin-1",
        updated_by="admin-1",
        created_at=now - 100,
        updated_at=now - 100,
    )
    created = create_protected_credential(
        ring,
        api_client_id=client.id,
        created_by="admin-1",
        credential_id="credential-http-1",
        key_id="zpk_metadata_http_000001",
        now=now - 100,
    )
    secret = created.take_secret()
    other = ApiClient(
        id="client-http-2",
        name="Other Caller",
        description=None,
        status="active",
        allowed_endpoints=["models.read", "usage.read"],
        allowed_models=["private-model"],
        group_ids=[],
        qps_limit=20,
        concurrency_limit=2,
        daily_request_limit=999,
        daily_token_limit=9_999,
        total_request_limit=9_999,
        total_token_limit=99_999,
        max_output_tokens_per_request=128,
        version=1,
        created_by="admin-1",
        updated_by="admin-1",
        created_at=now - 100,
        updated_at=now - 100,
    )
    rows = [
        ApiClientQuotaUsage(
            id="quota-http-daily-1",
            api_client_id=client.id,
            scope="daily",
            period_start=utc_day_start(now),
            request_count=3,
            token_reserved=11,
            token_consumed=19,
            version=1,
            updated_at=now - 5,
        ),
        ApiClientQuotaUsage(
            id="quota-http-total-1",
            api_client_id=client.id,
            scope="lifetime",
            period_start=0,
            request_count=20,
            token_reserved=11,
            token_consumed=89,
            version=1,
            updated_at=now - 5,
        ),
        ApiClientQuotaUsage(
            id="quota-http-daily-2",
            api_client_id=other.id,
            scope="daily",
            period_start=utc_day_start(now),
            request_count=777,
            token_reserved=777,
            token_consumed=777,
            version=1,
            updated_at=now - 5,
        ),
    ]
    with sessions() as session:
        session.add_all([client, created.credential, other, *rows])
        session.commit()

    redis = FakeRedis()
    app = FastAPI()
    app.state.external_authenticator = ExternalAuthenticator(
        keyring=ring,
        resolver=DatabaseCredentialResolver(sessions),
        timestamp_tolerance_seconds=300,
    )
    app.state.external_metadata_service = ExternalMetadataService(
        sessions=sessions,
        nonce_guard=NonceGuard(redis),
        qps_limiter=SlidingWindowQps(redis),
        clock=lambda: now,
    )
    app.include_router(router)
    return MetadataHttpRuntime(app=app, sessions=sessions, redis=redis, secret=secret, now=now)


def signed_headers(
    runtime: MetadataHttpRuntime,
    *,
    path: str,
    nonce: str,
    request_id: str,
    query: str = "",
    body: bytes = b"",
) -> dict[str, str]:
    timestamp = str(runtime.now)
    canonical = create_canonical_request(
        method="GET",
        raw_path=path,
        raw_query=query,
        body_hash=body_sha256_hex(body),
        key_id="zpk_metadata_http_000001",
        timestamp=timestamp,
        nonce=nonce,
        request_id=request_id,
    )
    return {
        "x-zangpu-key": "zpk_metadata_http_000001",
        "x-zangpu-timestamp": timestamp,
        "x-zangpu-nonce": nonce,
        "x-zangpu-request-id": request_id,
        "x-zangpu-signature-version": "1",
        "x-zangpu-signature": sign_canonical_request(runtime.secret, canonical),
    }


def test_signed_models_and_usage_are_isolated_and_side_effect_free() -> None:
    runtime = build_runtime()
    assert runtime.secret not in repr(runtime)
    with TestClient(runtime.app) as client:
        models = client.get(
            "/api/v1/external/models",
            headers=signed_headers(
                runtime,
                path="/api/v1/external/models",
                nonce="nonce_models_http_000001",
                request_id="req_models_http_000001",
            ),
        )
        usage = client.get(
            "/api/v1/external/usage",
            headers=signed_headers(
                runtime,
                path="/api/v1/external/usage",
                nonce="nonce_usage_http_000001",
                request_id="req_usage_http_000001",
            ),
        )
        with runtime.sessions() as session:
            operation_count = session.scalar(select(func.count()).select_from(ApiCallOperation))
            event_count = session.scalar(select(func.count()).select_from(ApiCallEvent))

    assert models.status_code == 200
    assert models.json() == {
        "object": "list",
        "data": [
            {"id": "model-1", "object": "model"},
            {"id": "model-2", "object": "model"},
        ],
    }
    assert usage.status_code == 200
    assert usage.json()["daily"]["request_count"] == 3
    assert usage.json()["daily"]["token_remaining"] == 70
    assert usage.json()["lifetime"]["request_count"] == 20
    assert "balance" not in json.dumps(usage.json()).lower()
    assert "private-model" not in models.text
    assert "777" not in usage.text
    assert (operation_count, event_count) == (0, 0)
    assert models.headers["x-zangpu-request-id"].startswith("req_")
    assert models.headers["x-zangpu-request-id"] != "req_models_http_000001"
    assert usage.headers["x-ratelimit-limit"] == "20"
    assert models.headers["cache-control"] == "no-store"


def test_metadata_nonce_replay_and_endpoint_permission_fail_closed() -> None:
    runtime = build_runtime()
    model_headers = signed_headers(
        runtime,
        path="/api/v1/external/models",
        nonce="nonce_models_http_000002",
        request_id="req_models_http_000002",
    )
    with TestClient(runtime.app) as client:
        first = client.get("/api/v1/external/models", headers=model_headers)
        replay = client.get("/api/v1/external/models", headers=model_headers)
        with runtime.sessions() as session:
            api_client = session.get(ApiClient, "client-http-1")
            assert api_client is not None
            api_client.allowed_endpoints = ["models.read"]
            session.commit()
        forbidden = client.get(
            "/api/v1/external/usage",
            headers=signed_headers(
                runtime,
                path="/api/v1/external/usage",
                nonce="nonce_usage_http_000002",
                request_id="req_usage_http_000002",
            ),
        )

    assert first.status_code == 200
    assert (replay.status_code, replay.json()["error"]["code"]) == (401, "AUTH_FAILED")
    assert (forbidden.status_code, forbidden.json()["error"]["code"]) == (403, "ENDPOINT_FORBIDDEN")


def test_metadata_rechecks_revoked_credential_and_disabled_client() -> None:
    runtime = build_runtime()
    with TestClient(runtime.app) as client:
        with runtime.sessions() as session:
            credential = session.get(ApiClientCredential, "credential-http-1")
            assert credential is not None
            credential.status = "revoked"
            session.commit()
        revoked = client.get(
            "/api/v1/external/models",
            headers=signed_headers(
                runtime,
                path="/api/v1/external/models",
                nonce="nonce_models_http_000003",
                request_id="req_models_http_000003",
            ),
        )
        with runtime.sessions() as session:
            credential = session.get(ApiClientCredential, "credential-http-1")
            api_client = session.get(ApiClient, "client-http-1")
            assert credential is not None and api_client is not None
            credential.status = "active"
            api_client.status = "disabled"
            session.commit()
        disabled = client.get(
            "/api/v1/external/models",
            headers=signed_headers(
                runtime,
                path="/api/v1/external/models",
                nonce="nonce_models_http_000004",
                request_id="req_models_http_000004",
            ),
        )

    assert (revoked.status_code, revoked.json()["error"]["code"]) == (401, "AUTH_FAILED")
    assert (disabled.status_code, disabled.json()["error"]["code"]) == (403, "CLIENT_DISABLED")


def test_metadata_signs_empty_body_and_exact_query_bytes() -> None:
    runtime = build_runtime()
    with TestClient(runtime.app) as client:
        tampered_query = client.get(
            "/api/v1/external/models?view=compact",
            headers=signed_headers(
                runtime,
                path="/api/v1/external/models",
                nonce="nonce_models_http_000005",
                request_id="req_models_http_000005",
            ),
        )
        signed_query = client.get(
            "/api/v1/external/models?view=compact",
            headers=signed_headers(
                runtime,
                path="/api/v1/external/models",
                query="view=compact",
                nonce="nonce_models_http_000006",
                request_id="req_models_http_000006",
            ),
        )
        nonempty_body = client.request(
            "GET",
            "/api/v1/external/models",
            content=b"{}",
            headers=signed_headers(
                runtime,
                path="/api/v1/external/models",
                body=b"{}",
                nonce="nonce_models_http_000007",
                request_id="req_models_http_000007",
            ),
        )

    assert (tampered_query.status_code, tampered_query.json()["error"]["code"]) == (401, "AUTH_FAILED")
    assert (signed_query.status_code, signed_query.json()["error"]["code"]) == (400, "INVALID_REQUEST")
    assert (nonempty_body.status_code, nonempty_body.json()["error"]["code"]) == (400, "INVALID_REQUEST")
