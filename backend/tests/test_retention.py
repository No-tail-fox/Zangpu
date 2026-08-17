import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base
from backend.app.models.audits import ApiClientAdminAudit
from backend.app.models.events import ApiCallEvent
from backend.app.services.retention import (
    RetentionConfirmationError,
    RetentionNothingToPurge,
    RetentionService,
    RetentionSnapshotConflict,
)

NOW = 100_000_000


def sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def terminal_event(event_id: str, created_at: int) -> ApiCallEvent:
    return ApiCallEvent(
        id=event_id,
        server_request_id=f"server-{event_id}",
        client_request_id=f"client-{event_id}",
        operation_id=None,
        api_client_id=None,
        credential_id=None,
        endpoint="chat.completions",
        method="POST",
        model_id="model-1",
        stream=False,
        outcome="success",
        stage="response",
        http_status=200,
        business_code="OK",
        retryable=False,
        duration_ms=10,
        quota_overrun=False,
        prompt_tokens=5,
        completion_tokens=5,
        total_tokens=10,
        charged_micro=5,
        qps_observed=1,
        concurrency_observed=1,
        daily_requests_after=1,
        daily_tokens_after=10,
        total_requests_after=1,
        total_tokens_after=10,
        remote_ip_hash=None,
        user_agent_family="SDK",
        started_at=created_at,
        completed_at=created_at,
        created_at=created_at,
    )


def admin_audit(audit_id: str, created_at: int) -> ApiClientAdminAudit:
    return ApiClientAdminAudit(
        id=audit_id,
        actor_user_id="admin",
        api_client_id=None,
        target_type="export",
        target_id=f"export-{audit_id}",
        action="events.exported",
        changed_fields=["row_count"],
        before_summary={},
        after_summary={"row_count": 1},
        created_at=created_at,
    )


def service(factory: sessionmaker, *, batch_size: int = 2) -> RetentionService:
    return RetentionService(
        factory,
        event_retention_days=30,
        admin_audit_retention_days=365,
        batch_size=batch_size,
    )


def test_retention_preview_and_purge_are_bounded_oldest_first_and_audited() -> None:
    factory = sessions()
    with factory.begin() as session:
        session.add_all(
            (
                terminal_event("event-old-1", 1),
                terminal_event("event-old-2", 2),
                terminal_event("event-old-3", 3),
                terminal_event("event-current", NOW),
                admin_audit("audit-old-1", 1),
                admin_audit("audit-old-2", 2),
                admin_audit("audit-current", NOW),
            )
        )
    retention = service(factory)

    preview = retention.preview(now=NOW)
    result = retention.purge(
        actor_id="admin",
        now=NOW,
        expected_event_count=preview.event_eligible_count,
        expected_audit_count=preview.audit_eligible_count,
        confirmed=True,
    )

    assert (preview.event_eligible_count, preview.audit_eligible_count) == (3, 2)
    assert (preview.event_batch_count, preview.audit_batch_count) == (2, 2)
    assert (result.event_deleted_count, result.audit_deleted_count) == (2, 2)
    assert (result.event_remaining_count, result.audit_remaining_count) == (1, 0)
    with factory() as session:
        event_ids = set(session.scalars(select(ApiCallEvent.id)))
        audits = list(session.scalars(select(ApiClientAdminAudit)))
    assert event_ids == {"event-old-3", "event-current"}
    assert {item.id for item in audits} == {"audit-current", result.audit_id}
    retention_audit = next(item for item in audits if item.id == result.audit_id)
    assert retention_audit.target_type == "retention"
    assert retention_audit.action == "retention.purged"
    assert retention_audit.after_summary["event_deleted_count"] == 2


def test_retention_requires_confirmation_and_exact_fresh_preview() -> None:
    factory = sessions()
    with factory.begin() as session:
        session.add(terminal_event("event-old-1", 1))
    retention = service(factory)
    preview = retention.preview(now=NOW)

    with pytest.raises(RetentionConfirmationError):
        retention.purge(
            actor_id="admin",
            now=NOW,
            expected_event_count=preview.event_eligible_count,
            expected_audit_count=preview.audit_eligible_count,
            confirmed=False,
        )

    with factory.begin() as session:
        session.add(terminal_event("event-old-2", 2))
    with pytest.raises(RetentionSnapshotConflict):
        retention.purge(
            actor_id="admin",
            now=NOW,
            expected_event_count=preview.event_eligible_count,
            expected_audit_count=preview.audit_eligible_count,
            confirmed=True,
        )

    with factory() as session:
        assert set(session.scalars(select(ApiCallEvent.id))) == {"event-old-1", "event-old-2"}
        assert session.scalar(select(ApiClientAdminAudit.id)) is None


def test_retention_rejects_empty_purge_without_writing_an_audit() -> None:
    factory = sessions()
    retention = service(factory)
    preview = retention.preview(now=NOW)

    with pytest.raises(RetentionNothingToPurge):
        retention.purge(
            actor_id="admin",
            now=NOW,
            expected_event_count=preview.event_eligible_count,
            expected_audit_count=preview.audit_eligible_count,
            confirmed=True,
        )

    with factory() as session:
        assert session.scalar(select(ApiClientAdminAudit.id)) is None
