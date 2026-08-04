import asyncio
import base64
import json
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.errors import ExternalApiError
from backend.app.limits.qps import QpsDecision
from backend.app.models import Base
from backend.app.models.clients import ApiClient
from backend.app.models.credentials import ApiClientCredential
from backend.app.models.quotas import ApiClientQuotaUsage
from backend.app.security.credentials import create_protected_credential
from backend.app.security.dependencies import AuthenticatedCaller
from backend.app.security.keyring import CredentialKeyring
from backend.app.services.metadata import ExternalMetadataService
from backend.app.services.quota import utc_day_start

NOW = 1_785_852_345


@pytest.fixture
def engine() -> Iterator[Engine]:
    value = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(value)
    yield value
    value.dispose()


def keyring() -> CredentialKeyring:
    encoded = base64.b64encode(bytes(range(32))).decode("ascii")
    return CredentialKeyring.from_json(SecretStr(json.dumps({"v1": encoded})), active_key_id="v1")


def seed_callers(engine: Engine) -> None:
    ring = keyring()
    clients = [
        ApiClient(
            id="client-1",
            name="Caller One",
            description=None,
            status="active",
            allowed_endpoints=["models.read", "usage.read"],
            allowed_models=["model-b", "model-a"],
            group_ids=[],
            qps_limit=10,
            concurrency_limit=2,
            daily_request_limit=10,
            daily_token_limit=100,
            total_request_limit=100,
            total_token_limit=1_000,
            max_output_tokens_per_request=128,
            version=1,
            created_by="admin-1",
            updated_by="admin-1",
            created_at=NOW - 100,
            updated_at=NOW - 100,
        ),
        ApiClient(
            id="client-2",
            name="Caller Two",
            description=None,
            status="active",
            allowed_endpoints=["models.read", "usage.read"],
            allowed_models=["private-model"],
            group_ids=[],
            qps_limit=5,
            concurrency_limit=1,
            daily_request_limit=999,
            daily_token_limit=9_999,
            total_request_limit=9_999,
            total_token_limit=99_999,
            max_output_tokens_per_request=64,
            version=1,
            created_by="admin-1",
            updated_by="admin-1",
            created_at=NOW - 100,
            updated_at=NOW - 100,
        ),
    ]
    credentials = [
        create_protected_credential(
            ring,
            api_client_id=client.id,
            created_by="admin-1",
            credential_id=f"credential-{index}",
            key_id=f"zpk_metadata_000000000{index}",
            now=NOW - 100,
        ).credential
        for index, client in enumerate(clients, start=1)
    ]
    quotas = [
        ApiClientQuotaUsage(
            id="quota-daily-1",
            api_client_id="client-1",
            scope="daily",
            period_start=utc_day_start(NOW),
            request_count=3,
            token_reserved=11,
            token_consumed=19,
            version=1,
            updated_at=NOW - 5,
        ),
        ApiClientQuotaUsage(
            id="quota-total-1",
            api_client_id="client-1",
            scope="lifetime",
            period_start=0,
            request_count=20,
            token_reserved=11,
            token_consumed=89,
            version=1,
            updated_at=NOW - 5,
        ),
        ApiClientQuotaUsage(
            id="quota-daily-2",
            api_client_id="client-2",
            scope="daily",
            period_start=utc_day_start(NOW),
            request_count=777,
            token_reserved=777,
            token_consumed=777,
            version=1,
            updated_at=NOW - 5,
        ),
    ]
    with Session(engine) as session:
        session.add_all([*clients, *credentials, *quotas])
        session.commit()


def caller(*, nonce: str = "nonce_metadata_0123456789") -> AuthenticatedCaller:
    return AuthenticatedCaller(
        api_client_id="client-1",
        credential_id="credential-1",
        key_id="zpk_metadata_0000000001",
        request_id="req_metadata_0123456789",
        nonce=nonce,
        timestamp=NOW,
    )


class FakeNonceGuard:
    def __init__(self, calls: list[str], *, claimed: bool = True) -> None:
        self.calls = calls
        self.claimed = claimed

    async def claim(self, **_kwargs: object) -> bool:
        self.calls.append("nonce")
        return self.claimed


class FakeQpsLimiter:
    def __init__(self, calls: list[str], *, allowed: bool = True) -> None:
        self.calls = calls
        self.allowed = allowed

    async def admit(self, **_kwargs: object) -> QpsDecision:
        self.calls.append("qps")
        return QpsDecision(self.allowed, 1, 10, 9, (NOW + 1) * 1_000, NOW * 1_000)


def build_service(
    engine: Engine,
    calls: list[str],
    *,
    nonce_claimed: bool = True,
    qps_allowed: bool = True,
) -> ExternalMetadataService:
    return ExternalMetadataService(
        sessions=sessionmaker(engine, expire_on_commit=False),
        nonce_guard=FakeNonceGuard(calls, claimed=nonce_claimed),
        qps_limiter=FakeQpsLimiter(calls, allowed=qps_allowed),
        clock=lambda: NOW,
    )


def test_models_and_usage_are_caller_scoped_and_read_only(engine: Engine) -> None:
    seed_callers(engine)
    calls: list[str] = []
    service = build_service(engine, calls)

    models = asyncio.run(
        service.list_models(caller=caller(), server_request_id="req_server_models_0123456789")
    )
    usage = asyncio.run(
        service.get_usage(
            caller=caller(nonce="nonce_metadata_9876543210"),
            server_request_id="req_server_usage_0123456789",
        )
    )

    assert models.response.model_dump(mode="json") == {
        "object": "list",
        "data": [
            {"id": "model-a", "object": "model"},
            {"id": "model-b", "object": "model"},
        ],
    }
    assert models.rate_limit_headers["X-RateLimit-Remaining"] == "9"
    assert usage.response.model_dump(mode="json") == {
        "object": "usage",
        "as_of": NOW,
        "daily": {
            "scope": "daily",
            "period_start": utc_day_start(NOW),
            "period_end": utc_day_start(NOW) + 86_400,
            "request_count": 3,
            "request_limit": 10,
            "request_remaining": 7,
            "token_consumed": 19,
            "token_reserved": 11,
            "token_limit": 100,
            "token_remaining": 70,
            "updated_at": NOW - 5,
        },
        "lifetime": {
            "scope": "lifetime",
            "period_start": 0,
            "period_end": None,
            "request_count": 20,
            "request_limit": 100,
            "request_remaining": 80,
            "token_consumed": 89,
            "token_reserved": 11,
            "token_limit": 1_000,
            "token_remaining": 900,
            "updated_at": NOW - 5,
        },
    }
    assert calls == ["nonce", "qps", "nonce", "qps"]


def test_metadata_rechecks_credential_and_client_state_after_auth(engine: Engine) -> None:
    seed_callers(engine)
    calls: list[str] = []
    service = build_service(engine, calls)
    with Session(engine) as session:
        credential = session.get(ApiClientCredential, "credential-1")
        assert credential is not None
        credential.status = "revoked"
        session.commit()

    with pytest.raises(ExternalApiError) as revoked:
        asyncio.run(service.list_models(caller=caller(), server_request_id="req_server_models_0123456789"))
    assert revoked.value.code == "AUTH_FAILED"

    with Session(engine) as session:
        credential = session.get(ApiClientCredential, "credential-1")
        client = session.get(ApiClient, "client-1")
        assert credential is not None and client is not None
        credential.status = "active"
        client.status = "disabled"
        session.commit()

    with pytest.raises(ExternalApiError) as disabled:
        asyncio.run(service.list_models(caller=caller(), server_request_id="req_server_models_9876543210"))
    assert disabled.value.code == "CLIENT_DISABLED"
    assert calls == ["nonce", "nonce"]


def test_metadata_enforces_nonce_permission_and_qps(engine: Engine) -> None:
    seed_callers(engine)
    with Session(engine) as session:
        client = session.get(ApiClient, "client-1")
        assert client is not None
        client.allowed_endpoints = ["models.read"]
        session.commit()

    permission_calls: list[str] = []
    with pytest.raises(ExternalApiError) as forbidden:
        asyncio.run(
            build_service(engine, permission_calls).get_usage(
                caller=caller(), server_request_id="req_server_usage_0123456789"
            )
        )
    assert forbidden.value.code == "ENDPOINT_FORBIDDEN"
    assert permission_calls == ["nonce"]

    replay_calls: list[str] = []
    with pytest.raises(ExternalApiError) as replayed:
        asyncio.run(
            build_service(engine, replay_calls, nonce_claimed=False).list_models(
                caller=caller(), server_request_id="req_server_models_0123456789"
            )
        )
    assert replayed.value.code == "AUTH_FAILED"
    assert replay_calls == ["nonce"]

    qps_calls: list[str] = []
    with pytest.raises(ExternalApiError) as limited:
        asyncio.run(
            build_service(engine, qps_calls, qps_allowed=False).list_models(
                caller=caller(), server_request_id="req_server_models_0123456789"
            )
        )
    assert limited.value.code == "QPS_LIMITED"
    assert limited.value.headers["X-RateLimit-Remaining"] == "9"
    assert qps_calls == ["nonce", "qps"]


def test_missing_daily_usage_is_zero_without_creating_a_quota_row(engine: Engine) -> None:
    seed_callers(engine)
    with Session(engine) as session:
        daily = session.get(ApiClientQuotaUsage, "quota-daily-1")
        client = session.get(ApiClient, "client-1")
        assert daily is not None and client is not None
        session.delete(daily)
        client.daily_request_limit = None
        client.daily_token_limit = None
        session.commit()

    result = asyncio.run(
        build_service(engine, []).get_usage(
            caller=caller(),
            server_request_id="req_server_usage_empty_0123456789",
        )
    )

    assert result.response.daily.model_dump(mode="json") == {
        "scope": "daily",
        "period_start": utc_day_start(NOW),
        "period_end": utc_day_start(NOW) + 86_400,
        "request_count": 0,
        "request_limit": None,
        "request_remaining": None,
        "token_consumed": 0,
        "token_reserved": 0,
        "token_limit": None,
        "token_remaining": None,
        "updated_at": None,
    }
    with Session(engine) as session:
        assert session.get(ApiClientQuotaUsage, "quota-daily-1") is None
