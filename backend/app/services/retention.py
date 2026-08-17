from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models.audits import ApiClientAdminAudit
from backend.app.models.base import new_uuid
from backend.app.models.events import ApiCallEvent

SECONDS_PER_DAY = 86_400
MIN_EVENT_RETENTION_DAYS = 30
MIN_AUDIT_RETENTION_DAYS = 365
MAX_RETENTION_DAYS = 3_650
MAX_RETENTION_BATCH_SIZE = 10_000


class RetentionConfirmationError(RuntimeError):
    pass


class RetentionSnapshotConflict(RuntimeError):
    pass


class RetentionNothingToPurge(RuntimeError):
    pass


class RetentionPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_event_count: int = Field(ge=0, le=2**63 - 1)
    expected_audit_count: int = Field(ge=0, le=2**63 - 1)
    confirmed: bool


class RetentionPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: int
    event_retention_days: int
    admin_audit_retention_days: int
    event_cutoff: int
    audit_cutoff: int
    event_eligible_count: int
    audit_eligible_count: int
    event_batch_count: int
    audit_batch_count: int
    batch_size: int


class RetentionPurgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str
    completed_at: int
    event_cutoff: int
    audit_cutoff: int
    event_deleted_count: int
    audit_deleted_count: int
    event_remaining_count: int
    audit_remaining_count: int
    batch_size: int


class RetentionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        event_retention_days: int,
        admin_audit_retention_days: int,
        batch_size: int,
    ) -> None:
        if not MIN_EVENT_RETENTION_DAYS <= event_retention_days <= MAX_RETENTION_DAYS:
            raise ValueError("event retention days are outside the supported range")
        if not MIN_AUDIT_RETENTION_DAYS <= admin_audit_retention_days <= MAX_RETENTION_DAYS:
            raise ValueError("administrator audit retention days are outside the supported range")
        if admin_audit_retention_days < event_retention_days:
            raise ValueError("administrator audit retention must not be shorter than event retention")
        if not 1 <= batch_size <= MAX_RETENTION_BATCH_SIZE:
            raise ValueError("retention batch size is outside the supported range")
        self._sessions = sessions
        self._event_retention_days = event_retention_days
        self._admin_audit_retention_days = admin_audit_retention_days
        self._batch_size = batch_size

    def preview(self, *, now: int) -> RetentionPreview:
        event_cutoff, audit_cutoff = self._cutoffs(now)
        with self._sessions() as session:
            event_count = self._event_count(session, event_cutoff)
            audit_count = self._audit_count(session, audit_cutoff)
        return self._preview(now, event_cutoff, audit_cutoff, event_count, audit_count)

    def purge(
        self,
        *,
        actor_id: str,
        now: int,
        expected_event_count: int,
        expected_audit_count: int,
        confirmed: bool,
    ) -> RetentionPurgeResult:
        if confirmed is not True:
            raise RetentionConfirmationError("retention purge requires explicit confirmation")
        if expected_event_count < 0 or expected_audit_count < 0:
            raise ValueError("expected retention counts must not be negative")
        event_cutoff, audit_cutoff = self._cutoffs(now)
        with self._sessions.begin() as session:
            event_count = self._event_count(session, event_cutoff)
            audit_count = self._audit_count(session, audit_cutoff)
            if (event_count, audit_count) != (expected_event_count, expected_audit_count):
                raise RetentionSnapshotConflict("retention preview is stale; preview again before purging")
            if event_count == 0 and audit_count == 0:
                raise RetentionNothingToPurge("no records are eligible for retention purge")

            event_ids = list(
                session.scalars(
                    select(ApiCallEvent.id)
                    .where(ApiCallEvent.created_at < event_cutoff)
                    .order_by(ApiCallEvent.created_at, ApiCallEvent.id)
                    .limit(self._batch_size)
                )
            )
            audit_ids = list(
                session.scalars(
                    select(ApiClientAdminAudit.id)
                    .where(ApiClientAdminAudit.created_at < audit_cutoff)
                    .order_by(ApiClientAdminAudit.created_at, ApiClientAdminAudit.id)
                    .limit(self._batch_size)
                )
            )

            connection = session.connection()
            event_deleted = self._delete_events(connection, event_ids)
            audit_deleted = self._delete_audits(connection, audit_ids)
            if event_deleted != len(event_ids) or audit_deleted != len(audit_ids):
                raise RetentionSnapshotConflict("retention rows changed while the purge was running")

            audit_id = new_uuid()
            before = {
                "event_eligible_count": event_count,
                "audit_eligible_count": audit_count,
            }
            after = {
                "event_deleted_count": event_deleted,
                "audit_deleted_count": audit_deleted,
                "event_remaining_count": event_count - event_deleted,
                "audit_remaining_count": audit_count - audit_deleted,
                "event_cutoff": event_cutoff,
                "audit_cutoff": audit_cutoff,
                "event_retention_days": self._event_retention_days,
                "admin_audit_retention_days": self._admin_audit_retention_days,
                "batch_size": self._batch_size,
            }
            session.add(
                ApiClientAdminAudit(
                    id=audit_id,
                    actor_user_id=actor_id,
                    api_client_id=None,
                    target_type="retention",
                    target_id=audit_id,
                    action="retention.purged",
                    changed_fields=[*before, *after],
                    before_summary=before,
                    after_summary=after,
                    created_at=now,
                )
            )
            session.flush()

        return RetentionPurgeResult(
            audit_id=audit_id,
            completed_at=now,
            event_cutoff=event_cutoff,
            audit_cutoff=audit_cutoff,
            event_deleted_count=event_deleted,
            audit_deleted_count=audit_deleted,
            event_remaining_count=event_count - event_deleted,
            audit_remaining_count=audit_count - audit_deleted,
            batch_size=self._batch_size,
        )

    def _cutoffs(self, now: int) -> tuple[int, int]:
        if now < 0:
            raise ValueError("retention time must not be negative")
        return (
            max(0, now - self._event_retention_days * SECONDS_PER_DAY),
            max(0, now - self._admin_audit_retention_days * SECONDS_PER_DAY),
        )

    def _preview(
        self,
        now: int,
        event_cutoff: int,
        audit_cutoff: int,
        event_count: int,
        audit_count: int,
    ) -> RetentionPreview:
        return RetentionPreview(
            generated_at=now,
            event_retention_days=self._event_retention_days,
            admin_audit_retention_days=self._admin_audit_retention_days,
            event_cutoff=event_cutoff,
            audit_cutoff=audit_cutoff,
            event_eligible_count=event_count,
            audit_eligible_count=audit_count,
            event_batch_count=min(event_count, self._batch_size),
            audit_batch_count=min(audit_count, self._batch_size),
            batch_size=self._batch_size,
        )

    @staticmethod
    def _event_count(session: Session, cutoff: int) -> int:
        return int(
            session.scalar(
                select(func.count()).select_from(ApiCallEvent).where(ApiCallEvent.created_at < cutoff)
            )
            or 0
        )

    @staticmethod
    def _audit_count(session: Session, cutoff: int) -> int:
        return int(
            session.scalar(
                select(func.count()).select_from(ApiClientAdminAudit).where(ApiClientAdminAudit.created_at < cutoff)
            )
            or 0
        )

    @staticmethod
    def _delete_events(connection: Connection, event_ids: list[str]) -> int:
        if not event_ids:
            return 0
        result = connection.execute(ApiCallEvent.__table__.delete().where(ApiCallEvent.id.in_(event_ids)))
        return int(result.rowcount)

    @staticmethod
    def _delete_audits(connection: Connection, audit_ids: list[str]) -> int:
        if not audit_ids:
            return 0
        result = connection.execute(
            ApiClientAdminAudit.__table__.delete().where(ApiClientAdminAudit.id.in_(audit_ids))
        )
        return int(result.rowcount)
