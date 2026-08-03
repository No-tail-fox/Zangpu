from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.models import Base
from backend.app.models.clients import ApiClient
from backend.app.models.credentials import ApiClientCredential
from backend.app.models.events import ApiCallEvent
from backend.app.models.operations import ApiCallOperation
from backend.app.models.quotas import ApiClientQuotaUsage
from backend.app.services.quota import (
    OperationTerminal,
    QuotaAdmissionError,
    finalize_operation,
    record_credit_reservation,
    record_provider_usage,
    reserve_operation,
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def seed_client(
    engine: Engine,
    *,
    daily_request_limit: int | None = 10,
    daily_token_limit: int | None = 1_000,
    total_request_limit: int | None = 100,
    total_token_limit: int | None = 10_000,
) -> None:
    with Session(engine) as session:
        session.add(
            ApiClient(
                id="client-1",
                name="Caller One",
                description=None,
                status="active",
                allowed_endpoints=["chat.completions"],
                allowed_models=["tibetan-med"],
                group_ids=[],
                qps_limit=10,
                concurrency_limit=2,
                daily_request_limit=daily_request_limit,
                daily_token_limit=daily_token_limit,
                total_request_limit=total_request_limit,
                total_token_limit=total_token_limit,
                max_output_tokens_per_request=128,
                version=1,
                created_by="admin-1",
                updated_by="admin-1",
                created_at=1_785_420_000,
                updated_at=1_785_420_000,
            )
        )
        session.add(
            ApiClientCredential(
                id="credential-1",
                api_client_id="client-1",
                key_id="zpk_test_0123456789",
                secret_ciphertext="ciphertext-sentinel",  # noqa: S106 - non-secret test sentinel
                secret_nonce="nonce-sentinel",  # noqa: S106 - non-secret test sentinel
                master_key_id="v1",
                secret_fingerprint="fingerprint-sentinel",  # noqa: S106 - non-secret test sentinel
                status="active",
                created_by="admin-1",
                created_at=1_785_420_000,
            )
        )
        session.commit()


def reserve(
    session: Session,
    *,
    operation_id: str = "10000000-0000-4000-8000-000000000001",
    client_request_id: str = "req_0123456789abcdef",
    request_fingerprint: str = "a" * 64,
    reserved_tokens: int = 100,
):
    return reserve_operation(
        session,
        api_client_id="client-1",
        credential_id="credential-1",
        operation_id=operation_id,
        client_request_id=client_request_id,
        request_fingerprint=request_fingerprint,
        model_id="tibetan-med",
        reserved_tokens=reserved_tokens,
        now=1_785_420_000,
    )


def terminal(**overrides: object) -> OperationTerminal:
    values: dict[str, object] = {
        "status": "completed",
        "outcome": "success",
        "stage": "response",
        "http_status": 200,
        "business_code": "OK",
        "retryable": False,
        "duration_ms": 1_357,
        "charged_micro": 20,
        "qps_observed": 1,
        "concurrency_observed": 1,
        "server_request_id": "req_server_0123456789abcdef",
        "remote_ip_hash": "b" * 64,
        "user_agent_family": "test-client",
        "completed_at": 1_785_420_001,
    }
    values.update(overrides)
    return OperationTerminal(**values)  # type: ignore[arg-type]


def quota_rows(session: Session) -> dict[str, ApiClientQuotaUsage]:
    rows = session.scalars(select(ApiClientQuotaUsage).where(ApiClientQuotaUsage.api_client_id == "client-1")).all()
    return {row.scope: row for row in rows}


def test_quota_reservation_and_terminalization_are_exact_once(engine: Engine) -> None:
    seed_client(engine)
    with Session(engine) as session, session.begin():
        snapshot = reserve(session)
        assert snapshot.daily_requests_after == 1
        assert snapshot.total_requests_after == 1

    with Session(engine) as session, session.begin():
        with pytest.raises(QuotaAdmissionError) as pending:
            reserve(session)
        assert pending.value.code == "REQUEST_IN_PROGRESS"
        assert pending.value.operation_id == "10000000-0000-4000-8000-000000000001"

    with Session(engine) as session, session.begin():
        with pytest.raises(QuotaAdmissionError) as conflict:
            reserve(session, request_fingerprint="c" * 64)
        assert conflict.value.code == "REQUEST_ID_CONFLICT"

    with Session(engine) as session, session.begin():
        record_credit_reservation(
            session,
            operation_id="10000000-0000-4000-8000-000000000001",
            settlement_id="20000000-0000-4000-8000-000000000002",
            usage_operation_id="10000000-0000-4000-8000-000000000001:usage",
            now=1_785_420_000,
        )
        record_provider_usage(
            session,
            operation_id="10000000-0000-4000-8000-000000000001",
            prompt_tokens=10,
            completion_tokens=5,
            now=1_785_420_001,
        )
        completed = finalize_operation(
            session,
            operation_id="10000000-0000-4000-8000-000000000001",
            terminal=terminal(),
        )
        assert completed.total_tokens == 15
        assert completed.daily_tokens_after == 15

    with Session(engine) as session, session.begin():
        repeated = finalize_operation(
            session,
            operation_id="10000000-0000-4000-8000-000000000001",
            terminal=terminal(),
        )
        assert repeated.total_tokens == 15

    with Session(engine) as session:
        rows = quota_rows(session)
        operation = session.get(ApiCallOperation, "10000000-0000-4000-8000-000000000001")
        events = session.scalars(select(ApiCallEvent)).all()
        assert rows["daily"].request_count == rows["lifetime"].request_count == 1
        assert rows["daily"].token_reserved == rows["lifetime"].token_reserved == 0
        assert rows["daily"].token_consumed == rows["lifetime"].token_consumed == 15
        assert operation is not None and operation.status == "completed"
        assert len(events) == 1 and events[0].charged_micro == 20
        assert events[0].duration_ms == 1_357
        assert events[0].quota_overrun is False

    with Session(engine) as session, session.begin():
        with pytest.raises(QuotaAdmissionError) as completed:
            reserve(session)
        assert completed.value.code == "REQUEST_ALREADY_COMPLETED"


def test_failed_provider_releases_tokens_but_keeps_admitted_request(engine: Engine) -> None:
    seed_client(engine)
    with Session(engine) as session, session.begin():
        reserve(session, reserved_tokens=250)
        result = finalize_operation(
            session,
            operation_id="10000000-0000-4000-8000-000000000001",
            terminal=terminal(
                status="rejected",
                outcome="provider_error",
                stage="provider",
                http_status=503,
                business_code="MODEL_UNAVAILABLE",
                retryable=True,
                charged_micro=0,
            ),
        )
        assert result.total_tokens == 0

    with Session(engine) as session:
        rows = quota_rows(session)
        assert rows["daily"].request_count == rows["lifetime"].request_count == 1
        assert rows["daily"].token_reserved == rows["lifetime"].token_reserved == 0
        assert rows["daily"].token_consumed == rows["lifetime"].token_consumed == 0


@pytest.mark.parametrize(
    ("limits", "reserved_tokens", "expected_code"),
    [
        ({"daily_request_limit": 1}, 10, "DAILY_REQUEST_QUOTA_EXCEEDED"),
        ({"total_request_limit": 1}, 10, "TOTAL_REQUEST_QUOTA_EXCEEDED"),
        ({"daily_token_limit": 109}, 10, "DAILY_TOKEN_QUOTA_EXCEEDED"),
        ({"total_token_limit": 109}, 10, "TOTAL_TOKEN_QUOTA_EXCEEDED"),
    ],
)
def test_quota_boundaries_reject_without_partial_mutation(
    engine: Engine,
    limits: dict[str, int],
    reserved_tokens: int,
    expected_code: str,
) -> None:
    seed_client(engine, **limits)
    with Session(engine) as session, session.begin():
        reserve(session, reserved_tokens=100)

    with Session(engine) as session:
        before = {
            scope: (row.request_count, row.token_reserved, row.token_consumed)
            for scope, row in quota_rows(session).items()
        }

    with Session(engine) as session, session.begin():
        with pytest.raises(QuotaAdmissionError) as captured:
            reserve(
                session,
                operation_id="10000000-0000-4000-8000-000000000009",
                client_request_id="req_9999999999999999",
                request_fingerprint="d" * 64,
                reserved_tokens=reserved_tokens,
            )
        assert captured.value.code == expected_code

    with Session(engine) as session:
        after = {
            scope: (row.request_count, row.token_reserved, row.token_consumed)
            for scope, row in quota_rows(session).items()
        }
        assert after == before
        assert session.get(ApiCallOperation, "10000000-0000-4000-8000-000000000009") is None


def test_actual_usage_can_exceed_reservation_without_losing_accounting_truth(engine: Engine) -> None:
    seed_client(engine)
    with Session(engine) as session, session.begin():
        reserve(session, reserved_tokens=10)
        record_provider_usage(
            session,
            operation_id="10000000-0000-4000-8000-000000000001",
            prompt_tokens=20,
            completion_tokens=15,
            now=1_785_420_001,
        )
        result = finalize_operation(
            session,
            operation_id="10000000-0000-4000-8000-000000000001",
            terminal=terminal(charged_micro=35),
        )
        assert result.total_tokens == 35
        assert result.quota_overrun is True

    with Session(engine) as session:
        rows = quota_rows(session)
        assert rows["daily"].token_reserved == 0
        assert rows["daily"].token_consumed == 35
        event = session.scalar(select(ApiCallEvent))
        assert event is not None and event.quota_overrun is True


@pytest.mark.parametrize(
    ("status", "expires_at", "expected_code"),
    [
        ("revoked", None, "CREDENTIAL_REVOKED"),
        ("active", 1_785_420_000, "CREDENTIAL_EXPIRED"),
    ],
)
def test_quota_reservation_rechecks_credential_state_atomically(
    engine: Engine,
    status: str,
    expires_at: int | None,
    expected_code: str,
) -> None:
    seed_client(engine)
    with Session(engine) as session, session.begin():
        credential = session.get(ApiClientCredential, "credential-1")
        assert credential is not None
        credential.status = status
        credential.expires_at = expires_at

    with Session(engine) as session, session.begin():
        with pytest.raises(QuotaAdmissionError) as captured:
            reserve(session)
        assert captured.value.code == expected_code

    with Session(engine) as session:
        assert quota_rows(session) == {}
        assert session.get(ApiCallOperation, "10000000-0000-4000-8000-000000000001") is None
