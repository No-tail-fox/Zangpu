import asyncio
import base64
import json
from collections.abc import Iterator
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.errors import ExternalApiError
from backend.app.api.external_models import ChatCompletionRequest
from backend.app.integrations.bifrost.binding_service import persist_created_binding
from backend.app.integrations.bifrost.models import (
    BifrostUpstreamError,
    ChatChoice,
    ChatCompletionResponse,
    ChatMessage,
    ChatUsage,
    VirtualKeyCreationResult,
    VirtualKeyState,
)
from backend.app.integrations.openwebui.models import OpenWebUIUpstreamError
from backend.app.limits.concurrency import ConcurrencyDecision
from backend.app.limits.qps import QpsDecision
from backend.app.models import Base
from backend.app.models.bindings import ApiClientBinding
from backend.app.models.clients import ApiClient
from backend.app.models.events import ApiCallEvent
from backend.app.models.operations import ApiCallOperation
from backend.app.models.quotas import ApiClientQuotaUsage
from backend.app.security.credentials import create_protected_credential
from backend.app.security.dependencies import AuthenticatedCaller
from backend.app.security.keyring import CredentialKeyring
from backend.app.services.chat import ExternalChatService

VIRTUAL_KEY_VALUE = "vk-chat-redaction-sentinel"
SERVICE_USER_ID = "10000000-0000-4000-8000-000000000001"


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


def seed_caller(engine: Engine) -> CredentialKeyring:
    ring = keyring()
    client = ApiClient(
        id="client-1",
        name="Caller One",
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
    credential = create_protected_credential(
        ring,
        api_client_id=client.id,
        created_by="admin-1",
        credential_id="credential-1",
        key_id="zpk_chat_0123456789",
        now=1_785_420_000,
    ).credential
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
    with Session(engine) as session:
        session.add_all((client, credential, binding))
        session.commit()
    return ring


def request_model() -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {
            "model": "model-1",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
            "max_tokens": 32,
        }
    )


def caller(*, nonce: str = "nonce_chat_0123456789") -> AuthenticatedCaller:
    return AuthenticatedCaller(
        api_client_id="client-1",
        credential_id="credential-1",
        key_id="zpk_chat_0123456789",
        request_id="req_chat_0123456789",
        nonce=nonce,
        timestamp=1_785_420_100,
    )


class FakeNonceGuard:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def claim(self, **_kwargs: object) -> bool:
        self.calls.append("nonce")
        return True


class FakeQpsLimiter:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def admit(self, **_kwargs: object) -> QpsDecision:
        self.calls.append("qps")
        return QpsDecision(True, 1, 10, 9, 1_785_420_101_000, 1_785_420_100_000)


class FakeConcurrencyLimiter:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def acquire(self, **_kwargs: object) -> ConcurrencyDecision:
        self.calls.append("concurrency_acquire")
        return ConcurrencyDecision(True, 1, 2, 1, 1_785_420_160_000, 1_785_420_100_000)

    async def release(self, **_kwargs: object) -> bool:
        self.calls.append("concurrency_release")
        return True


class FakeBifrost:
    def __init__(self, calls: list[str], *, failure: bool = False) -> None:
        self.calls = calls
        self.failure = failure
        self.forward_count = 0

    async def forward_chat_completion(
        self, _payload: dict[str, object], *, virtual_key: SecretStr
    ) -> ChatCompletionResponse:
        assert virtual_key.get_secret_value() == VIRTUAL_KEY_VALUE
        self.calls.append("bifrost")
        self.forward_count += 1
        if self.failure:
            raise BifrostUpstreamError(code="BIFROST_UNAVAILABLE", status_code=503, retryable=True)
        return ChatCompletionResponse(
            id="chatcmpl-1",
            model="model-1",
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content="answer"),
                    finish_reason="stop",
                )
            ],
            usage=ChatUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        )


class FakeOpenWebUI:
    def __init__(
        self,
        calls: list[str],
        *,
        reserve_error: str | None = None,
        settle_error: str | None = None,
        identity_mismatch: bool = False,
    ) -> None:
        self.calls = calls
        self.reserve_error = reserve_error
        self.settle_error = settle_error
        self.identity_mismatch = identity_mismatch

    async def reserve_operation(
        self,
        *,
        operation_id: str,
        service_user_id: str,
        model_id: str,
        provider: str,
    ):
        assert UUID(operation_id)
        assert service_user_id == SERVICE_USER_ID
        self.calls.append("credit_reserve")
        if self.reserve_error is not None:
            raise OpenWebUIUpstreamError(code=self.reserve_error, status_code=402, retryable=False)
        return SimpleNamespace(
            operation_id=UUID("90000000-0000-4000-8000-000000000009")
            if self.identity_mismatch
            else UUID(operation_id),
            settlement_id=UUID("20000000-0000-4000-8000-000000000002"),
            service_user_id=UUID(service_user_id),
            model_id=model_id,
            provider=provider,
            usage_operation_id=f"{operation_id}:usage",
            status="pending",
            charged_micro=0,
        )

    async def settle_operation(
        self,
        *,
        operation_id: str,
        service_user_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        **_kwargs: object,
    ):
        self.calls.append("credit_settle")
        if self.settle_error is not None:
            raise OpenWebUIUpstreamError(code=self.settle_error, status_code=503, retryable=True)
        return SimpleNamespace(
            operation_id=UUID(operation_id),
            settlement_id=UUID("20000000-0000-4000-8000-000000000002"),
            service_user_id=UUID(service_user_id),
            model_id="model-1",
            provider="bifrost",
            usage_operation_id=f"{operation_id}:usage",
            status="succeeded_charged",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            charged_micro=20,
        )

    async def cancel_operation(self, *, operation_id: str, service_user_id: str):
        self.calls.append("credit_cancel")
        return SimpleNamespace(
            operation_id=UUID(operation_id),
            settlement_id=UUID("20000000-0000-4000-8000-000000000002"),
            service_user_id=UUID(service_user_id),
            status="cancelled_charged",
            charged_micro=0,
        )


def build_service(
    engine: Engine,
    ring: CredentialKeyring,
    *,
    calls: list[str],
    bifrost: FakeBifrost | None = None,
    openwebui: FakeOpenWebUI | None = None,
) -> tuple[ExternalChatService, FakeBifrost]:
    remote = bifrost or FakeBifrost(calls)
    return (
        ExternalChatService(
            sessions=sessionmaker(engine, expire_on_commit=False),
            keyring=ring,
            nonce_guard=FakeNonceGuard(calls),
            qps_limiter=FakeQpsLimiter(calls),
            concurrency_limiter=FakeConcurrencyLimiter(calls),
            bifrost=remote,  # type: ignore[arg-type]
            openwebui=openwebui or FakeOpenWebUI(calls),  # type: ignore[arg-type]
            global_max_output_tokens=256,
        ),
        remote,
    )


def execute(service: ExternalChatService, *, current_caller: AuthenticatedCaller | None = None):
    return asyncio.run(
        service.execute(
            request=request_model(),
            caller=current_caller or caller(),
            server_request_id="req_server_0123456789",
            request_fingerprint="a" * 64,
        )
    )


def test_non_stream_success_settles_credit_quota_event_and_releases_lease(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    service, _remote = build_service(engine, ring, calls=calls)

    result = execute(service)

    assert result.response.usage.total_tokens == 8
    assert result.rate_limit_headers["X-RateLimit-Remaining"] == "9"
    assert calls == [
        "nonce",
        "qps",
        "concurrency_acquire",
        "credit_reserve",
        "bifrost",
        "credit_settle",
        "concurrency_release",
    ]
    with Session(engine) as session:
        operation = session.scalar(select(ApiCallOperation))
        event = session.scalar(select(ApiCallEvent))
        quotas = {row.scope: row for row in session.scalars(select(ApiClientQuotaUsage)).all()}
        assert operation is not None and operation.status == "completed"
        assert operation.total_tokens == 8 and operation.credit_settlement_id is not None
        assert event is not None and (event.outcome, event.stage, event.charged_micro) == ("success", "response", 20)
        assert quotas["daily"].token_reserved == 0 and quotas["daily"].token_consumed == 8


def test_completed_request_id_never_calls_model_or_charges_twice(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    service, remote = build_service(engine, ring, calls=calls)
    execute(service)

    with pytest.raises(ExternalApiError) as captured:
        execute(service, current_caller=caller(nonce="nonce_retry_0123456789"))

    assert captured.value.code == "REQUEST_ALREADY_COMPLETED"
    assert captured.value.operation_id is not None
    assert remote.forward_count == 1
    assert calls.count("credit_reserve") == calls.count("credit_settle") == 1
    assert calls.count("concurrency_release") == 2


def test_credit_rejection_terminalizes_without_calling_provider(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    credit = FakeOpenWebUI(calls, reserve_error="CREDIT_BALANCE_EXHAUSTED")
    service, remote = build_service(engine, ring, calls=calls, openwebui=credit)

    with pytest.raises(ExternalApiError) as captured:
        execute(service)

    assert captured.value.code == "CREDIT_BALANCE_EXHAUSTED"
    assert remote.forward_count == 0
    assert calls[-2:] == ["credit_reserve", "concurrency_release"]
    with Session(engine) as session:
        operation = session.scalar(select(ApiCallOperation))
        event = session.scalar(select(ApiCallEvent))
        quota = session.scalar(select(ApiClientQuotaUsage).where(ApiClientQuotaUsage.scope == "daily"))
        assert operation is not None and operation.status == "rejected"
        assert event is not None and (event.stage, event.http_status) == ("credit", 402)
        assert quota is not None and quota.token_reserved == quota.token_consumed == 0


def test_provider_failure_cancels_credit_before_terminalization_and_release(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    remote = FakeBifrost(calls, failure=True)
    service, _remote = build_service(engine, ring, calls=calls, bifrost=remote)

    with pytest.raises(ExternalApiError) as captured:
        execute(service)

    assert captured.value.code == "MODEL_UNAVAILABLE"
    assert calls[-3:] == ["bifrost", "credit_cancel", "concurrency_release"]
    with Session(engine) as session:
        operation = session.scalar(select(ApiCallOperation))
        event = session.scalar(select(ApiCallEvent))
        assert operation is not None and operation.status == "rejected"
        assert event is not None and (event.outcome, event.stage, event.total_tokens) == (
            "provider_error",
            "provider",
            0,
        )


def test_settlement_uncertainty_keeps_recoverable_operation_and_releases_lease(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    credit = FakeOpenWebUI(calls, settle_error="OPENWEBUI_UNAVAILABLE")
    service, _remote = build_service(engine, ring, calls=calls, openwebui=credit)

    with pytest.raises(ExternalApiError) as captured:
        execute(service)

    assert captured.value.code == "CONTROL_PLANE_UNAVAILABLE"
    assert calls[-3:] == ["bifrost", "credit_settle", "concurrency_release"]
    assert "credit_cancel" not in calls
    with Session(engine) as session:
        operation = session.scalar(select(ApiCallOperation))
        assert operation is not None and operation.status == "pending"
        assert operation.total_tokens == 8 and operation.credit_settlement_id is not None
        assert session.scalar(select(ApiCallEvent)) is None


def test_mismatched_credit_reservation_identity_never_reaches_provider(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    credit = FakeOpenWebUI(calls, identity_mismatch=True)
    service, remote = build_service(engine, ring, calls=calls, openwebui=credit)

    with pytest.raises(ExternalApiError) as captured:
        execute(service)

    assert captured.value.code == "CONTROL_PLANE_UNAVAILABLE"
    assert remote.forward_count == 0
    assert calls[-2:] == ["credit_reserve", "concurrency_release"]
    with Session(engine) as session:
        operation = session.scalar(select(ApiCallOperation))
        assert operation is not None and operation.status == "pending"
        assert operation.credit_settlement_id is None
