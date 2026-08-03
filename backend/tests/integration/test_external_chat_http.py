import base64
import json
import time
from collections.abc import Callable
from uuid import UUID

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import DatabaseRuntime
from backend.app.integrations.bifrost.binding_service import persist_created_binding
from backend.app.integrations.bifrost.client import BifrostClient
from backend.app.integrations.bifrost.models import VirtualKeyCreationResult, VirtualKeyState
from backend.app.integrations.openwebui.client import OpenWebUIClient
from backend.app.main import create_app
from backend.app.models import Base
from backend.app.models.bindings import ApiClientBinding
from backend.app.models.clients import ApiClient
from backend.app.models.events import ApiCallEvent
from backend.app.models.operations import ApiCallOperation
from backend.app.models.quotas import ApiClientQuotaUsage
from backend.app.security.canonical import (
    body_sha256_hex,
    create_canonical_request,
    sign_canonical_request,
)
from backend.app.security.credentials import create_protected_credential
from backend.app.security.keyring import CredentialKeyring
from backend.app.settings import Settings

SERVICE_USER_ID = "10000000-0000-4000-8000-000000000001"
SETTLEMENT_ID = "20000000-0000-4000-8000-000000000002"
ACCOUNT_ID = "30000000-0000-4000-8000-000000000003"
VIRTUAL_KEY_VALUE = "vk-http-integration-redaction-sentinel"


def keyring_configuration() -> tuple[str, CredentialKeyring]:
    serialized = json.dumps({"v1": base64.b64encode(bytes(range(32))).decode("ascii")})
    return serialized, CredentialKeyring.from_json(SecretStr(serialized), active_key_id="v1")


def seed_runtime() -> tuple[DatabaseRuntime, str, str]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    runtime = DatabaseRuntime(engine=engine, sessions=sessionmaker(engine, expire_on_commit=False))
    serialized_keys, ring = keyring_configuration()
    client = ApiClient(
        id="client-1",
        name="HTTP Caller",
        description=None,
        status="active",
        allowed_endpoints=["chat.completions"],
        allowed_models=["model-1"],
        group_ids=[],
        qps_limit=10,
        concurrency_limit=2,
        daily_request_limit=100,
        daily_token_limit=100_000,
        total_request_limit=1_000,
        total_token_limit=1_000_000,
        max_output_tokens_per_request=128,
        version=1,
        created_by="admin-1",
        updated_by="admin-1",
        created_at=1_785_420_000,
        updated_at=1_785_420_000,
    )
    created = create_protected_credential(
        ring,
        api_client_id=client.id,
        created_by="admin-1",
        credential_id="credential-1",
        key_id="zpk_http_0123456789",
        now=1_785_420_000,
    )
    caller_secret = created.take_secret()
    binding = ApiClientBinding(
        id="binding-1",
        api_client_id=client.id,
        zangpu_service_user_id=SERVICE_USER_ID,
        bifrost_virtual_key_id=None,
        bifrost_value_ciphertext=None,
        bifrost_value_key_version=None,
        bifrost_config_hash="desired-config-hash",
        sync_status="pending",
        version=1,
        created_at=1_785_420_000,
        updated_at=1_785_420_000,
    )
    persist_created_binding(
        binding,
        VirtualKeyCreationResult(
            state=VirtualKeyState(
                id="vk-1",
                name="zangpu-client-1",
                description="managed by Zangpu",
                is_active=True,
                provider="provider-1",
                model="model-1",
                config_hash="remote-config-hash",
            ),
            value=SecretStr(VIRTUAL_KEY_VALUE),
        ),
        ring,
        now=1_785_420_000,
    )
    with runtime.sessions() as session:
        session.add_all((client, created.credential, binding))
        session.commit()
    return runtime, serialized_keys, caller_secret


def settings(serialized_keys: str) -> Settings:
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
        api_credential_keys=serialized_keys,
        api_credential_active_key_id="v1",
    )


def signed_headers(body: bytes, *, secret: str, nonce: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    canonical = create_canonical_request(
        method="POST",
        raw_path="/api/v1/external/chat/completions",
        raw_query="",
        body_hash=body_sha256_hex(body),
        key_id="zpk_http_0123456789",
        timestamp=timestamp,
        nonce=nonce,
        request_id="req_http_0123456789",
    )
    return {
        "content-type": "application/json",
        "x-zangpu-key": "zpk_http_0123456789",
        "x-zangpu-timestamp": timestamp,
        "x-zangpu-nonce": nonce,
        "x-zangpu-request-id": "req_http_0123456789",
        "x-zangpu-signature-version": "1",
        "x-zangpu-signature": sign_canonical_request(secret, canonical),
    }


def openwebui_handler(calls: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        operation_id = payload["operation_id"]
        common = {
            "operation_id": operation_id,
            "settlement_id": SETTLEMENT_ID,
            "service_user_id": SERVICE_USER_ID,
            "model_id": "model-1",
            "provider": "bifrost",
            "account": {
                "account_id": ACCOUNT_ID,
                "service_user_id": SERVICE_USER_ID,
                "balance_micro": 10_000_000,
                "status": "active",
                "version": 2,
                "updated_at": 1_785_420_100,
            },
            "started_at": 1_785_420_100,
            "updated_at": 1_785_420_100,
            "usage_operation_id": f"{operation_id}:usage",
        }
        if request.url.path.endswith("/reserve"):
            calls.append("reserve")
            return httpx.Response(
                200,
                json={
                    **common,
                    "status": "pending",
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "charged_micro": 0,
                    "balance_after_micro": None,
                    "account_version_after": None,
                    "completed_at": None,
                },
            )
        if request.url.path.endswith("/settle"):
            calls.append("settle")
            prompt_tokens = payload["prompt_tokens"]
            completion_tokens = payload["completion_tokens"]
            return httpx.Response(
                200,
                json={
                    **common,
                    "status": "succeeded_charged",
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "charged_micro": 20,
                    "balance_after_micro": 9_999_980,
                    "account_version_after": 2,
                    "completed_at": 1_785_420_100,
                },
            )
        raise AssertionError(f"unexpected Open WebUI path: {request.url.path}")

    return handler


def test_signed_http_lifecycle_is_exact_once_across_all_local_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, serialized_keys, caller_secret = seed_runtime()
    redis = FakeRedis()
    monkeypatch.setattr("backend.app.main.create_redis_client", lambda _url: redis)
    bifrost_calls: list[str] = []
    credit_calls: list[str] = []

    def bifrost_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-bf-vk"] == VIRTUAL_KEY_VALUE
        bifrost_calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-http-1",
                "model": "model-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            },
        )

    async def successful_preflight(_client: BifrostClient, _version: str) -> None:
        return None

    application = create_app(
        settings(serialized_keys),
        database_factory=lambda _settings: runtime,
        bifrost_client_factory=lambda active: BifrostClient(
            base_url=str(active.bifrost_base_url),
            management_token=active.bifrost_management_token,
            transport=httpx.MockTransport(bifrost_handler),
        ),
        bifrost_preflight=successful_preflight,
        openwebui_client_factory=lambda active: OpenWebUIClient(
            base_url=str(active.openwebui_internal_base_url),
            service_id=active.openwebui_internal_service_id,
            service_secret=active.openwebui_internal_service_secret,
            transport=httpx.MockTransport(openwebui_handler(credit_calls)),
        ),
    )
    body = json.dumps(
        {
            "model": "model-1",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
            "max_tokens": 32,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    with TestClient(application) as client:
        first = client.post(
            "/api/v1/external/chat/completions",
            content=body,
            headers=signed_headers(body, secret=caller_secret, nonce="nonce_http_0123456789"),
        )
        repeated = client.post(
            "/api/v1/external/chat/completions",
            content=body,
            headers=signed_headers(body, secret=caller_secret, nonce="nonce_http_9876543210"),
        )

        with runtime.sessions() as session:
            operation_count = session.scalar(select(func.count()).select_from(ApiCallOperation))
            event_count = session.scalar(select(func.count()).select_from(ApiCallEvent))
            daily = session.scalar(select(ApiClientQuotaUsage).where(ApiClientQuotaUsage.scope == "daily"))

    assert first.status_code == 200
    assert first.json()["usage"]["total_tokens"] == 8
    assert (repeated.status_code, repeated.json()["error"]["code"]) == (
        409,
        "REQUEST_ALREADY_COMPLETED",
    )
    assert repeated.json()["error"]["operation_id"]
    assert bifrost_calls == ["/v1/chat/completions"]
    assert credit_calls == ["reserve", "settle"]
    assert (operation_count, event_count) == (1, 1)
    assert daily is not None and (daily.request_count, daily.token_reserved, daily.token_consumed) == (1, 0, 8)
    assert UUID(repeated.json()["error"]["operation_id"])
