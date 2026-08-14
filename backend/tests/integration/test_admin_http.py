import base64
import json

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import DatabaseRuntime
from backend.app.integrations.bifrost.client import BifrostClient
from backend.app.integrations.openwebui.client import OpenWebUIClient
from backend.app.main import create_app
from backend.app.models import Base
from backend.app.models.credentials import ApiClientCredential
from backend.app.models.events import ApiCallEvent
from backend.app.services.observability import AdminObservabilityService
from backend.app.settings import Settings


def runtime() -> tuple[DatabaseRuntime, str]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    keys = json.dumps({"v1": base64.b64encode(bytes(range(32))).decode("ascii")})
    return DatabaseRuntime(engine=engine, sessions=sessionmaker(engine, expire_on_commit=False)), keys


def settings(keys: str) -> Settings:
    return Settings(
        environment="test",
        service_version="0.1.0",
        database_url="postgresql+psycopg://unused:unused@postgres:5432/control",
        redis_url="redis://redis:6379/0",
        bifrost_base_url="http://bifrost:8080",
        bifrost_management_token="bifrost-management-token-that-is-at-least-32-bytes",  # noqa: S106
        bifrost_expected_version="v1.6.3",
        openwebui_internal_base_url="http://openwebui:8080",
        openwebui_internal_service_id="zangpu-api-control-plane",
        openwebui_internal_service_secret="openwebui-internal-secret-that-is-at-least-32-bytes",  # noqa: S106
        admin_session_secret="admin-session-secret-that-is-at-least-32-bytes",  # noqa: S106
        admin_login_token="admin-login-token-that-is-at-least-32-bytes",  # noqa: S106
        api_credential_keys=keys,
        api_credential_active_key_id="v1",
    )


def caller_payload() -> dict[str, object]:
    return {
        "name": "API 调用方",
        "description": "HTTP administrator contract",
        "service_user_id": "10000000-0000-4000-8000-000000000001",
        "provider": "provider-1",
        "model": "model-1",
        "allowed_endpoints": ["chat.completions", "models.read", "usage.read"],
        "allowed_models": ["model-1"],
        "group_ids": [],
        "qps_limit": 10,
        "concurrency_limit": 2,
        "daily_request_limit": 100,
        "daily_token_limit": 10_000,
        "total_request_limit": 1_000,
        "total_token_limit": 100_000,
        "max_output_tokens_per_request": 128,
    }


def build_application(monkeypatch: pytest.MonkeyPatch, database: DatabaseRuntime, keys: str):
    redis = FakeRedis()
    monkeypatch.setattr("backend.app.main.create_redis_client", lambda _url: redis)

    async def successful_preflight(_client: BifrostClient, _version: str) -> None:
        return None

    def no_remote_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"unused": True})

    return create_app(
        settings(keys),
        database_factory=lambda _settings: database,
        bifrost_client_factory=lambda active: BifrostClient(
            base_url=str(active.bifrost_base_url),
            management_token=active.bifrost_management_token,
            transport=httpx.MockTransport(no_remote_request),
        ),
        bifrost_preflight=successful_preflight,
        openwebui_client_factory=lambda active: OpenWebUIClient(
            base_url=str(active.openwebui_internal_base_url),
            service_id=active.openwebui_internal_service_id,
            service_secret=active.openwebui_internal_service_secret,
            transport=httpx.MockTransport(no_remote_request),
        ),
    )


def terminal_event(
    event_id: str,
    *,
    client_id: str,
    created_at: int,
    outcome: str = "success",
    model_id: str = "model-1",
) -> ApiCallEvent:
    success = outcome == "success"
    return ApiCallEvent(
        id=event_id,
        server_request_id=f"server-{event_id}",
        client_request_id=f"client-{event_id}",
        operation_id=None,
        api_client_id=client_id,
        credential_id=None,
        endpoint="chat.completions",
        method="POST",
        model_id=model_id,
        stream=False,
        outcome=outcome,
        stage="response" if success else "quota",
        http_status=200 if success else 429,
        business_code="OK" if success else "DAILY_REQUEST_QUOTA_EXCEEDED",
        retryable=not success,
        duration_ms=10 if success else 20,
        quota_overrun=not success,
        prompt_tokens=5 if success else 0,
        completion_tokens=5 if success else 0,
        total_tokens=10 if success else 0,
        charged_micro=5 if success else 0,
        qps_observed=1,
        concurrency_observed=1,
        daily_requests_after=1,
        daily_tokens_after=10 if success else 0,
        total_requests_after=1,
        total_tokens_after=10 if success else 0,
        remote_ip_hash="a" * 64,
        user_agent_family="SDK",
        started_at=created_at,
        completed_at=created_at + 1,
        created_at=created_at,
    )


def test_admin_http_session_and_caller_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    database, keys = runtime()
    application = build_application(monkeypatch, database, keys)

    with TestClient(application) as client:
        assert client.get("/api/v1/admin/callers").status_code == 401
        bad_login = client.post(
            "/api/v1/admin/session",
            headers={"x-zangpu-admin-token": "wrong-token-that-is-at-least-32-bytes"},
        )
        login = client.post(
            "/api/v1/admin/session",
            headers={"x-zangpu-admin-token": "admin-login-token-that-is-at-least-32-bytes"},
        )
        csrf = login.json()["csrf_token"]

        no_csrf = client.post(
            "/api/v1/admin/callers",
            headers={"idempotency-key": "http-create-1"},
            json=caller_payload(),
        )
        created = client.post(
            "/api/v1/admin/callers",
            headers={"x-zangpu-csrf": csrf, "idempotency-key": "http-create-1"},
            json=caller_payload(),
        )
        created_payload = created.json()
        client_id = created_payload["client"]["id"]
        first_secret = created_payload["secret"]
        replay_payload = caller_payload()
        replay_payload["name"] = "另一个调用方"
        replayed = client.post(
            "/api/v1/admin/callers",
            headers={"x-zangpu-csrf": csrf, "idempotency-key": "http-create-1"},
            json=replay_payload,
        )
        listed = client.get("/api/v1/admin/callers")
        detail = client.get(f"/api/v1/admin/callers/{client_id}")
        updated = client.patch(
            f"/api/v1/admin/callers/{client_id}",
            headers={"x-zangpu-csrf": csrf},
            json={
                "expected_version": created_payload["client"]["version"],
                "allowed_endpoints": ["chat.completions", "models.read"],
                "qps_limit": 20,
                "daily_request_limit": None,
            },
        )
        stale = client.patch(
            f"/api/v1/admin/callers/{client_id}",
            headers={"x-zangpu-csrf": csrf},
            json={"expected_version": 1, "qps_limit": 30},
        )
        rotated = client.post(
            f"/api/v1/admin/callers/{client_id}/credentials/rotate",
            headers={"x-zangpu-csrf": csrf},
        )
        revoked = client.post(
            f"/api/v1/admin/callers/{client_id}/credentials/{rotated.json()['credential']['id']}/revoke",
            headers={"x-zangpu-csrf": csrf},
        )
        disabled = client.post(
            f"/api/v1/admin/callers/{client_id}/disable",
            headers={"x-zangpu-csrf": csrf, "idempotency-key": "http-disable-1"},
        )
        with database.sessions() as session:
            credentials = session.scalars(select(ApiClientCredential)).all()

    assert (bad_login.status_code, bad_login.json()["error"]["code"]) == (401, "ADMIN_AUTH_FAILED")
    assert login.status_code == 200 and "httponly" in login.headers["set-cookie"].lower()
    assert "admin-login-token" not in repr(login.json())
    assert (no_csrf.status_code, no_csrf.json()["error"]["code"]) == (403, "ADMIN_CSRF_FAILED")
    assert created.status_code == 201 and first_secret.startswith("zps_")
    assert (replayed.status_code, replayed.json()["error"]["code"]) == (409, "ADMIN_CONFLICT")
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    assert detail.status_code == 200
    assert "secret" not in repr(detail.json()).lower()
    assert "ciphertext" not in repr(detail.json()).lower()
    assert updated.status_code == 200
    assert updated.json()["client"]["qps_limit"] == 20
    assert updated.json()["client"]["daily_request_limit"] is None
    assert updated.json()["client"]["allowed_endpoints"] == ["chat.completions", "models.read"]
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "ADMIN_CALLER_CONFLICT")
    assert rotated.status_code == 201 and rotated.json()["secret"].startswith("zps_")
    assert rotated.json()["secret"] != first_secret
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    assert disabled.status_code == 200 and disabled.json()["client"]["status"] == "disabled"
    assert len(credentials) == 2 and {item.status for item in credentials} == {"revoked"}


def test_admin_http_observability_is_authenticated_bounded_and_csv_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, keys = runtime()
    application = build_application(monkeypatch, database, keys)

    with TestClient(application) as client:
        unauthenticated = client.get("/api/v1/admin/events")
        login = client.post(
            "/api/v1/admin/session",
            headers={"x-zangpu-admin-token": "admin-login-token-that-is-at-least-32-bytes"},
        )
        csrf = login.json()["csrf_token"]
        created = client.post(
            "/api/v1/admin/callers",
            headers={"x-zangpu-csrf": csrf, "idempotency-key": "http-events-create-1"},
            json=caller_payload(),
        )
        client_id = created.json()["client"]["id"]
        with database.sessions.begin() as session:
            session.add_all(
                (
                    terminal_event("event-http-1", client_id=client_id, created_at=3_600),
                    terminal_event(
                        "event-http-2",
                        client_id=client_id,
                        created_at=7_200,
                        outcome="rejected",
                        model_id="=SUM(1,1)",
                    ),
                )
            )

        listed = client.get(f"/api/v1/admin/events?api_client_id={client_id}&limit=1")
        summary = client.get("/api/v1/admin/events/summary?bucket_seconds=3600")
        invalid_range = client.get("/api/v1/admin/events?created_from=7200&created_to=3600")
        invalid_bucket = client.get("/api/v1/admin/events/summary?bucket_seconds=600")
        export_without_csrf = client.post("/api/v1/admin/events/export")
        exported = client.post("/api/v1/admin/events/export", headers={"x-zangpu-csrf": csrf})

        application.state.admin_observability = AdminObservabilityService(
            database.sessions,
            max_export_rows=1,
            max_aggregate_rows=1,
        )
        broad_summary = client.get("/api/v1/admin/events/summary")
        broad_export = client.post("/api/v1/admin/events/export", headers={"x-zangpu-csrf": csrf})

    assert (unauthenticated.status_code, unauthenticated.json()["error"]["code"]) == (
        401,
        "ADMIN_AUTH_REQUIRED",
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 2 and listed.json()["items"][0]["id"] == "event-http-2"
    assert summary.status_code == 200
    assert (summary.json()["request_count"], summary.json()["quota_overrun_count"]) == (2, 1)
    assert (invalid_range.status_code, invalid_range.json()["error"]["code"]) == (422, "ADMIN_INVALID_FILTER")
    assert (invalid_bucket.status_code, invalid_bucket.json()["error"]["code"]) == (422, "ADMIN_INVALID_FILTER")
    assert (export_without_csrf.status_code, export_without_csrf.json()["error"]["code"]) == (
        403,
        "ADMIN_CSRF_FAILED",
    )
    assert exported.status_code == 200 and exported.headers["content-type"].startswith("text/csv")
    assert "attachment;" in exported.headers["content-disposition"]
    assert "'=SUM(1,1)" in exported.text
    assert "prompt" not in exported.text.lower()
    assert (broad_summary.status_code, broad_summary.json()["error"]["code"]) == (
        422,
        "ADMIN_OBSERVABILITY_LIMIT",
    )
    assert (broad_export.status_code, broad_export.json()["error"]["code"]) == (
        422,
        "ADMIN_OBSERVABILITY_LIMIT",
    )
