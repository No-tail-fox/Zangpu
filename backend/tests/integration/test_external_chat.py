import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
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
from backend.app.limits.model_pool import ModelPoolDecision, ModelPoolPolicy
from backend.app.limits.qps import QpsDecision
from backend.app.limits.redis import ControlPlaneUnavailable
from backend.app.models import Base
from backend.app.models.bindings import ApiClientBinding
from backend.app.models.clients import ApiClient
from backend.app.models.events import ApiCallEvent
from backend.app.models.operations import ApiCallOperation
from backend.app.models.quotas import ApiClientQuotaUsage
from backend.app.security.credentials import create_protected_credential
from backend.app.security.dependencies import AuthenticatedCaller
from backend.app.security.keyring import CredentialKeyring
from backend.app.services.chat import ExternalChatResult, ExternalChatService
from backend.app.workers.recovery import ExternalChatRecoveryWorker

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


def stream_request_model() -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {
            "model": "model-1",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
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
    def __init__(
        self,
        calls: list[str],
        *,
        allowed: bool = True,
        heartbeat_renewed: bool = True,
    ) -> None:
        self.calls = calls
        self.allowed = allowed
        self.heartbeat_renewed = heartbeat_renewed

    async def acquire(self, **_kwargs: object) -> ConcurrencyDecision:
        self.calls.append("concurrency_acquire")
        count = 1 if self.allowed else 2
        return ConcurrencyDecision(
            self.allowed,
            count,
            2,
            max(2 - count, 0),
            1_785_420_160_000,
            1_785_420_100_000,
        )

    async def release(self, **_kwargs: object) -> bool:
        self.calls.append("concurrency_release")
        return True

    async def heartbeat(self, **_kwargs: object) -> bool:
        self.calls.append("concurrency_heartbeat")
        return self.heartbeat_renewed


def model_pool_decision(status: str) -> ModelPoolDecision:
    queued = status == "queued"
    full = status in {"queue_full", "caller_queue_full"}
    return ModelPoolDecision(
        status=status,  # type: ignore[arg-type]
        active_count=1,
        active_limit=1,
        active_remaining=0,
        queue_count=1 if queued else (2 if full else 0),
        queue_limit=2,
        queue_remaining=1 if queued else (0 if full else 2),
        pool_queue_count=1 if queued else (2 if full else 0),
        queue_position=1 if queued else 0,
        expires_at_ms=1_785_420_160_000,
        observed_at_ms=1_785_420_100_000,
    )


class FakeModelPoolLimiter:
    def __init__(
        self,
        calls: list[str],
        *,
        decisions: list[ModelPoolDecision] | None = None,
        track: bool = False,
        heartbeat_renewed: bool = True,
    ) -> None:
        self.calls = calls
        self.decisions = list(decisions or [model_pool_decision("admitted")])
        self.track = track
        self.heartbeat_renewed = heartbeat_renewed

    async def admit_or_enqueue(self, **_kwargs: object) -> ModelPoolDecision:
        if self.track:
            self.calls.append("model_pool_admit")
        if len(self.decisions) > 1:
            return self.decisions.pop(0)
        return self.decisions[0]

    async def heartbeat(self, **_kwargs: object) -> bool:
        if self.track:
            self.calls.append("model_pool_heartbeat")
        return self.heartbeat_renewed

    async def release(self, **_kwargs: object) -> bool:
        if self.track:
            self.calls.append("model_pool_release")
        return True

    async def cancel(self, **_kwargs: object) -> bool:
        if self.track:
            self.calls.append("model_pool_cancel")
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


class FakeStreamingBifrost:
    def __init__(
        self,
        calls: list[str],
        *,
        fail_after_first: bool = False,
        include_done: bool = True,
    ) -> None:
        self.calls = calls
        self.fail_after_first = fail_after_first
        self.include_done = include_done
        self.forward_count = 0

    async def stream_chat_completion(
        self,
        _payload: dict[str, object],
        *,
        virtual_key: SecretStr,
    ) -> AsyncIterator[bytes]:
        assert virtual_key.get_secret_value() == VIRTUAL_KEY_VALUE
        self.calls.append("bifrost_stream")
        self.forward_count += 1
        yield (
            b'data: {"id":"chatcmpl-stream-1","object":"chat.completion.chunk",'
            b'"model":"model-1","choices":[{"index":0,"delta":{"content":"answer"}}]}\n\n'
        )
        await asyncio.sleep(0.02)
        if self.fail_after_first:
            raise BifrostUpstreamError(code="BIFROST_UNAVAILABLE", status_code=503, retryable=True)
        yield (
            b'data: {"id":"chatcmpl-stream-1","object":"chat.completion.chunk",'
            b'"model":"model-1","choices":[],"usage":{"prompt_tokens":5,'
            b'"completion_tokens":3,"total_tokens":8}}\n\n'
        )
        if self.include_done:
            yield b"data: [DONE]\n\n"


class FakeOpenWebUI:
    def __init__(
        self,
        calls: list[str],
        *,
        reserve_error: str | None = None,
        settle_error: str | None = None,
        identity_mismatch: bool = False,
        status_value: str = "pending",
        status_error: str | None = None,
    ) -> None:
        self.calls = calls
        self.reserve_error = reserve_error
        self.settle_error = settle_error
        self.identity_mismatch = identity_mismatch
        self.status_value = status_value
        self.status_error = status_error

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
            model_id="model-1",
            provider="bifrost",
            usage_operation_id=f"{operation_id}:usage",
            status="cancelled_charged",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            charged_micro=0,
        )

    async def get_operation_status(self, *, operation_id: str, service_user_id: str):
        self.calls.append("credit_status")
        if self.status_error is not None:
            raise OpenWebUIUpstreamError(code=self.status_error, status_code=404, retryable=False)
        terminal = self.status_value != "pending"
        return SimpleNamespace(
            operation_id=UUID(operation_id),
            settlement_id=UUID("20000000-0000-4000-8000-000000000002"),
            service_user_id=UUID(service_user_id),
            model_id="model-1",
            provider="bifrost",
            usage_operation_id=f"{operation_id}:usage",
            status=self.status_value,
            prompt_tokens=5 if terminal else 0,
            completion_tokens=3 if terminal else 0,
            total_tokens=8 if terminal else 0,
            charged_micro=20 if self.status_value == "succeeded_charged" else 0,
        )


def build_service(
    engine: Engine,
    ring: CredentialKeyring,
    *,
    calls: list[str],
    bifrost: FakeBifrost | FakeStreamingBifrost | None = None,
    openwebui: FakeOpenWebUI | None = None,
    concurrency_limiter: FakeConcurrencyLimiter | None = None,
    model_pool_limiter: FakeModelPoolLimiter | None = None,
    heartbeat_interval_seconds: float = 15.0,
    monotonic_seconds: Callable[[], float] = time.monotonic,
    queue_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[ExternalChatService, FakeBifrost]:
    remote = bifrost or FakeBifrost(calls)
    return (
        ExternalChatService(
            sessions=sessionmaker(engine, expire_on_commit=False),
            keyring=ring,
            nonce_guard=FakeNonceGuard(calls),
            qps_limiter=FakeQpsLimiter(calls),
            concurrency_limiter=concurrency_limiter or FakeConcurrencyLimiter(calls),
            model_pool_limiter=model_pool_limiter or FakeModelPoolLimiter(calls),  # type: ignore[arg-type]
            model_pool_policies={"model-1": ModelPoolPolicy(pool_id="pool-1", active_limit=1)},
            bifrost=remote,  # type: ignore[arg-type]
            openwebui=openwebui or FakeOpenWebUI(calls),  # type: ignore[arg-type]
            global_max_output_tokens=256,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            monotonic_seconds=monotonic_seconds,
            global_queue_limit=2,
            caller_queue_limit=1,
            queue_wait_seconds=30,
            queue_poll_milliseconds=50,
            queue_sleep=queue_sleep,
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


async def consume_stream(service: ExternalChatService) -> bytes:
    result = await service.prepare_stream(
        request=stream_request_model(),
        caller=caller(),
        server_request_id="req_server_stream_0123456789",
        request_fingerprint="b" * 64,
    )
    return b"".join([chunk async for chunk in result.stream])


def test_non_stream_success_settles_credit_quota_event_and_releases_lease(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    service, _remote = build_service(engine, ring, calls=calls)

    result = execute(service)

    assert result.response.usage.total_tokens == 8
    assert result.rate_limit_headers["X-RateLimit-Remaining"] == "9"
    assert result.rate_limit_headers["X-Concurrency-Limit"] == "2"
    assert result.rate_limit_headers["X-Concurrency-Remaining"] == "1"
    assert result.rate_limit_headers["X-Concurrency-Reset"] == "1785420160"
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


def test_concurrency_rejection_returns_stable_headers_before_quota_or_inference(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    concurrency = FakeConcurrencyLimiter(calls, allowed=False)
    service, remote = build_service(engine, ring, calls=calls, concurrency_limiter=concurrency)

    with pytest.raises(ExternalApiError) as captured:
        execute(service)

    assert captured.value.code == "CONCURRENCY_LIMITED"
    assert captured.value.headers == {
        "X-RateLimit-Limit": "10",
        "X-RateLimit-Remaining": "9",
        "X-RateLimit-Reset": "1785420101",
        "X-Model-Pool-Limit": "1",
        "X-Model-Pool-Remaining": "0",
        "X-Queue-Limit": "2",
        "X-Queue-Remaining": "2",
        "X-Model-Pool-Reset": "1785420160",
        "X-Concurrency-Limit": "2",
        "X-Concurrency-Remaining": "0",
        "X-Concurrency-Reset": "1785420160",
        "Retry-After": "60",
    }
    assert calls == ["nonce", "qps", "concurrency_acquire"]
    assert remote.forward_count == 0
    with Session(engine) as session:
        assert session.scalar(select(ApiCallOperation)) is None
        assert session.scalar(select(ApiCallEvent)) is None


def test_model_pool_waits_before_caller_quota_credit_or_inference(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    model_pool = FakeModelPoolLimiter(
        calls,
        decisions=[model_pool_decision("queued"), model_pool_decision("admitted")],
        track=True,
    )

    async def queue_sleep(_seconds: float) -> None:
        calls.append("queue_wait")
        assert "concurrency_acquire" not in calls
        assert "credit_reserve" not in calls
        assert "bifrost" not in calls

    service, _remote = build_service(
        engine,
        ring,
        calls=calls,
        model_pool_limiter=model_pool,
        queue_sleep=queue_sleep,
    )

    result = execute(service)

    assert result.response.usage.total_tokens == 8
    assert calls[:6] == [
        "nonce",
        "qps",
        "model_pool_admit",
        "queue_wait",
        "model_pool_admit",
        "concurrency_acquire",
    ]
    assert calls[-2:] == ["concurrency_release", "model_pool_release"]


def test_model_pool_full_returns_429_evidence_without_side_effects(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    model_pool = FakeModelPoolLimiter(
        calls,
        decisions=[model_pool_decision("queue_full")],
        track=True,
    )
    service, remote = build_service(engine, ring, calls=calls, model_pool_limiter=model_pool)

    with pytest.raises(ExternalApiError) as captured:
        execute(service)

    assert captured.value.code == "MODEL_CAPACITY_LIMITED"
    assert captured.value.headers == {
        "X-RateLimit-Limit": "10",
        "X-RateLimit-Remaining": "9",
        "X-RateLimit-Reset": "1785420101",
        "X-Model-Pool-Limit": "1",
        "X-Model-Pool-Remaining": "0",
        "X-Queue-Limit": "2",
        "X-Queue-Remaining": "0",
        "X-Model-Pool-Reset": "1785420160",
        "Retry-After": "60",
    }
    assert calls == ["nonce", "qps", "model_pool_admit"]
    assert remote.forward_count == 0
    with Session(engine) as session:
        assert session.scalar(select(ApiCallOperation)) is None
        assert session.scalar(select(ApiCallEvent)) is None


def test_caller_rejection_releases_already_acquired_model_slot(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    model_pool = FakeModelPoolLimiter(calls, track=True)
    concurrency = FakeConcurrencyLimiter(calls, allowed=False)
    service, remote = build_service(
        engine,
        ring,
        calls=calls,
        concurrency_limiter=concurrency,
        model_pool_limiter=model_pool,
    )

    with pytest.raises(ExternalApiError) as captured:
        execute(service)

    assert captured.value.code == "CONCURRENCY_LIMITED"
    assert calls == [
        "nonce",
        "qps",
        "model_pool_admit",
        "concurrency_acquire",
        "model_pool_release",
    ]
    assert remote.forward_count == 0


def test_busy_non_stream_renews_lease_until_provider_completes(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []

    class SlowBifrost(FakeBifrost):
        async def forward_chat_completion(
            self, payload: dict[str, object], *, virtual_key: SecretStr
        ) -> ChatCompletionResponse:
            await asyncio.sleep(0.02)
            return await super().forward_chat_completion(payload, virtual_key=virtual_key)

    remote = SlowBifrost(calls)
    model_pool = FakeModelPoolLimiter(calls, track=True)
    service, _remote = build_service(
        engine,
        ring,
        calls=calls,
        bifrost=remote,
        model_pool_limiter=model_pool,
        heartbeat_interval_seconds=0.005,
    )

    result = execute(service)

    assert result.response.usage.total_tokens == 8
    assert calls.count("concurrency_heartbeat") >= 1
    assert calls.count("model_pool_heartbeat") >= 1
    assert calls.index("concurrency_heartbeat") < calls.index("credit_settle")
    assert calls[-2:] == ["concurrency_release", "model_pool_release"]


def test_non_stream_lost_lease_cancels_provider_and_releases(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []

    class BlockingBifrost(FakeBifrost):
        cancelled = False

        async def forward_chat_completion(
            self, _payload: dict[str, object], *, virtual_key: SecretStr
        ) -> ChatCompletionResponse:
            assert virtual_key.get_secret_value() == VIRTUAL_KEY_VALUE
            self.calls.append("bifrost")
            self.forward_count += 1
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("blocking provider was not cancelled")

    class ProviderLeaseLimiter(FakeConcurrencyLimiter):
        async def heartbeat(self, **_kwargs: object) -> bool:
            self.calls.append("concurrency_heartbeat")
            return "bifrost" not in self.calls

    concurrency = ProviderLeaseLimiter(calls)
    model_pool = FakeModelPoolLimiter(calls, track=True)
    remote = BlockingBifrost(calls)
    service, _remote = build_service(
        engine,
        ring,
        calls=calls,
        bifrost=remote,
        concurrency_limiter=concurrency,
        model_pool_limiter=model_pool,
        heartbeat_interval_seconds=0.005,
    )

    async def scenario() -> ExternalChatResult:
        return await asyncio.wait_for(
            service.execute(
                request=request_model(),
                caller=caller(),
                server_request_id="req_server_lost_lease_0123456789",
                request_fingerprint="d" * 64,
            ),
            timeout=0.2,
        )

    with pytest.raises(ControlPlaneUnavailable):
        asyncio.run(scenario())

    assert remote.cancelled
    assert calls.count("concurrency_heartbeat") >= 1
    assert calls.index("bifrost") < len(calls) - 1 - calls[::-1].index("concurrency_heartbeat")
    assert calls.count("concurrency_release") == 1
    assert calls.count("model_pool_release") == 1
    assert "credit_settle" not in calls and "credit_cancel" not in calls


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


def test_stream_success_heartbeats_settles_before_done_and_records_stream_event(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    remote = FakeStreamingBifrost(calls)
    model_pool = FakeModelPoolLimiter(calls, track=True)
    service, _remote = build_service(
        engine,
        ring,
        calls=calls,
        bifrost=remote,
        model_pool_limiter=model_pool,
        heartbeat_interval_seconds=0.005,
    )

    body = asyncio.run(consume_stream(service))

    assert b'"content":"answer"' in body
    assert b": heartbeat\n\n" in body
    assert body.endswith(b"data: [DONE]\n\n")
    assert calls.index("credit_settle") < calls.index("concurrency_release")
    assert calls.count("concurrency_heartbeat") >= 1
    assert calls.count("model_pool_heartbeat") >= 1
    assert calls[-2:] == ["concurrency_release", "model_pool_release"]
    with Session(engine) as session:
        operation = session.scalar(select(ApiCallOperation))
        event = session.scalar(select(ApiCallEvent))
        assert operation is not None and (operation.status, operation.total_tokens) == ("completed", 8)
        assert event is not None and (event.stream, event.outcome, event.charged_micro) == (True, "success", 20)


def test_busy_stream_renews_lease_on_absolute_heartbeat_schedule(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []

    class BusyStreamingBifrost(FakeStreamingBifrost):
        async def stream_chat_completion(
            self,
            _payload: dict[str, object],
            *,
            virtual_key: SecretStr,
        ) -> AsyncIterator[bytes]:
            assert virtual_key.get_secret_value() == VIRTUAL_KEY_VALUE
            self.calls.append("bifrost_stream")
            self.forward_count += 1
            for index in range(8):
                yield (
                    b'data: {"id":"chatcmpl-busy-1","object":"chat.completion.chunk",'
                    b'"model":"model-1","choices":[{"index":0,"delta":{"content":"'
                    + str(index).encode("ascii")
                    + b'"}}]}\n\n'
                )
            yield (
                b'data: {"id":"chatcmpl-busy-1","object":"chat.completion.chunk",'
                b'"model":"model-1","choices":[],"usage":{"prompt_tokens":5,'
                b'"completion_tokens":3,"total_tokens":8}}\n\n'
                b"data: [DONE]\n\n"
            )

    clock_value = 0.0

    def stepped_clock() -> float:
        nonlocal clock_value
        clock_value += 0.6
        return clock_value

    remote = BusyStreamingBifrost(calls)
    service, _remote = build_service(
        engine,
        ring,
        calls=calls,
        bifrost=remote,
        heartbeat_interval_seconds=1.0,
        monotonic_seconds=stepped_clock,
    )

    body = asyncio.run(consume_stream(service))

    assert body.endswith(b"data: [DONE]\n\n")
    assert calls.count("concurrency_heartbeat") >= 1


def test_stream_provider_failure_cancels_unknown_usage_and_emits_sanitized_error(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    remote = FakeStreamingBifrost(calls, fail_after_first=True)
    service, _remote = build_service(
        engine,
        ring,
        calls=calls,
        bifrost=remote,
        heartbeat_interval_seconds=0.005,
    )

    body = asyncio.run(consume_stream(service))

    assert b'"code":"MODEL_UNAVAILABLE"' in body
    assert not body.endswith(b"data: [DONE]\n\n")
    assert calls[-2:] == ["credit_cancel", "concurrency_release"]
    with Session(engine) as session:
        event = session.scalar(select(ApiCallEvent))
        assert event is not None and (event.stream, event.outcome, event.total_tokens) == (
            True,
            "provider_error",
            0,
        )


def test_stream_disconnect_cancels_without_usage_and_releases_exactly_once(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    remote = FakeStreamingBifrost(calls)
    model_pool = FakeModelPoolLimiter(calls, track=True)
    service, _remote = build_service(
        engine,
        ring,
        calls=calls,
        bifrost=remote,
        model_pool_limiter=model_pool,
    )

    async def scenario() -> bytes:
        result = await service.prepare_stream(
            request=stream_request_model(),
            caller=caller(),
            server_request_id="req_server_disconnect_0123456789",
            request_fingerprint="c" * 64,
        )
        stream = result.stream
        first = await anext(stream)
        await stream.aclose()  # type: ignore[attr-defined]
        return first

    first = asyncio.run(scenario())

    assert b'"content":"answer"' in first
    assert calls.count("credit_cancel") == calls.count("concurrency_release") == 1
    assert calls.count("model_pool_release") == 1
    assert "credit_settle" not in calls
    with Session(engine) as session:
        event = session.scalar(select(ApiCallEvent))
        assert event is not None and (event.stream, event.outcome, event.stage) == (
            True,
            "cancelled",
            "response",
        )


def test_stale_recovery_settles_exact_local_usage_without_replaying_provider(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    credit = FakeOpenWebUI(calls, settle_error="OPENWEBUI_UNAVAILABLE")
    service, remote = build_service(engine, ring, calls=calls, openwebui=credit)
    with pytest.raises(ExternalApiError):
        execute(service)
    credit.settle_error = None
    model_calls_before = remote.forward_count

    worker = ExternalChatRecoveryWorker(
        sessionmaker(engine, expire_on_commit=False),
        credit,  # type: ignore[arg-type]
        stale_after_seconds=30,
        batch_size=10,
    )
    processed = asyncio.run(worker.run_once(now=int(time.time()) + 1_000))

    assert processed == 1
    assert remote.forward_count == model_calls_before == 1
    assert calls[-2:] == ["credit_status", "credit_settle"]
    with Session(engine) as session:
        operation = session.scalar(select(ApiCallOperation))
        event = session.scalar(select(ApiCallEvent))
        assert operation is not None and (
            operation.status,
            operation.provider_usage_recorded,
            operation.total_tokens,
        ) == ("completed", True, 8)
        assert event is not None and (event.stream, event.stage, event.outcome) == (
            False,
            "recovery",
            "success",
        )


def test_stale_recovery_cancels_unknown_stream_usage_without_starting_provider(engine: Engine) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    credit = FakeOpenWebUI(calls)
    remote = FakeStreamingBifrost(calls)
    service, _remote = build_service(engine, ring, calls=calls, bifrost=remote, openwebui=credit)

    async def admit_only():
        return await service.prepare_stream(
            request=stream_request_model(),
            caller=caller(),
            server_request_id="req_server_stale_0123456789",
            request_fingerprint="d" * 64,
        )

    result = asyncio.run(admit_only())
    worker = ExternalChatRecoveryWorker(
        sessionmaker(engine, expire_on_commit=False),
        credit,  # type: ignore[arg-type]
        stale_after_seconds=30,
        batch_size=10,
    )
    processed = asyncio.run(worker.run_once(now=int(time.time()) + 1_000))

    assert processed == 1
    assert remote.forward_count == 0
    assert calls[-2:] == ["credit_status", "credit_cancel"]
    with Session(engine) as session:
        operation = session.scalar(select(ApiCallOperation))
        event = session.scalar(select(ApiCallEvent))
        assert operation is not None and (
            operation.status,
            operation.stream,
            operation.provider_usage_recorded,
        ) == ("abandoned", True, False)
        assert event is not None and (event.stream, event.total_tokens, event.charged_micro) == (
            True,
            0,
            0,
        )
    asyncio.run(result.stream.aclose())  # type: ignore[attr-defined]


def test_stale_recovery_404_without_local_credit_evidence_abandons_without_mutating_remote(
    engine: Engine,
) -> None:
    ring = seed_caller(engine)
    calls: list[str] = []
    credit = FakeOpenWebUI(calls, status_error="OPENWEBUI_CREDIT_NOT_FOUND")
    service, _remote = build_service(engine, ring, calls=calls, openwebui=credit)

    async def admit_only():
        return await service.prepare_stream(
            request=stream_request_model(),
            caller=caller(),
            server_request_id="req_server_404_0123456789",
            request_fingerprint="e" * 64,
        )

    result = asyncio.run(admit_only())
    with Session(engine) as session:
        operation = session.scalar(select(ApiCallOperation))
        assert operation is not None
        operation.credit_settlement_id = None
        operation.usage_operation_id = None
        session.commit()

    worker = ExternalChatRecoveryWorker(
        sessionmaker(engine, expire_on_commit=False),
        credit,  # type: ignore[arg-type]
        stale_after_seconds=30,
        batch_size=10,
    )
    assert asyncio.run(worker.run_once(now=int(time.time()) + 1_000)) == 1

    assert calls[-1] == "credit_status"
    with Session(engine) as session:
        operation = session.scalar(select(ApiCallOperation))
        event = session.scalar(select(ApiCallEvent))
        assert operation is not None and (
            operation.status,
            operation.provider_usage_recorded,
            operation.total_tokens,
        ) == ("abandoned", False, 0)
        assert event is not None and event.business_code == "RECOVERED_NO_CREDIT"
    asyncio.run(result.stream.aclose())  # type: ignore[attr-defined]
