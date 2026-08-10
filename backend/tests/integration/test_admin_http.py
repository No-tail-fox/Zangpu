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


def test_admin_http_session_and_caller_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    database, keys = runtime()
    redis = FakeRedis()
    monkeypatch.setattr("backend.app.main.create_redis_client", lambda _url: redis)

    async def successful_preflight(_client: BifrostClient, _version: str) -> None:
        return None

    def no_remote_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"unused": True})

    application = create_app(
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
