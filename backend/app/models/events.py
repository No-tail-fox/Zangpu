from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column, relationship

from backend.app.models.base import Base, new_uuid, utc_epoch_seconds

if TYPE_CHECKING:
    from backend.app.models.clients import ApiClient
    from backend.app.models.credentials import ApiClientCredential
    from backend.app.models.operations import ApiCallOperation


EVENT_OUTCOMES = ("success", "cancelled", "rejected", "provider_error", "system_error", "abandoned")
EVENT_STAGES = ("request", "auth", "permission", "rate_limit", "quota", "credit", "provider", "response", "recovery")


class ImmutableEventError(RuntimeError):
    pass


class ApiCallEvent(Base):
    __tablename__ = "api_call_event"
    __table_args__ = (
        UniqueConstraint("server_request_id", name="uq_api_call_event_server_request_id"),
        UniqueConstraint("operation_id", name="uq_api_call_event_operation_id"),
        CheckConstraint(f"outcome IN {EVENT_OUTCOMES!r}", name="outcome_values"),
        CheckConstraint(f"stage IN {EVENT_STAGES!r}", name="stage_values"),
        CheckConstraint("http_status BETWEEN 100 AND 599", name="http_status"),
        CheckConstraint("duration_ms >= 0", name="duration_nonnegative"),
        CheckConstraint("prompt_tokens >= 0", name="prompt_tokens_nonnegative"),
        CheckConstraint("completion_tokens >= 0", name="completion_tokens_nonnegative"),
        CheckConstraint("total_tokens = prompt_tokens + completion_tokens", name="token_total_matches"),
        CheckConstraint("charged_micro >= 0", name="charged_micro_nonnegative"),
        CheckConstraint("qps_observed >= 0", name="qps_nonnegative"),
        CheckConstraint("concurrency_observed >= 0", name="concurrency_nonnegative"),
        CheckConstraint("daily_requests_after >= 0", name="daily_requests_nonnegative"),
        CheckConstraint("daily_tokens_after >= 0", name="daily_tokens_nonnegative"),
        CheckConstraint("total_requests_after >= 0", name="total_requests_nonnegative"),
        CheckConstraint("total_tokens_after >= 0", name="total_tokens_after_nonnegative"),
        CheckConstraint("completed_at >= started_at", name="completion_order"),
        Index("ix_api_call_event_api_client_created", "api_client_id", "created_at", "id"),
        Index("ix_api_call_event_created", "created_at", "id"),
        Index("ix_api_call_event_credential_id", "credential_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    server_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_call_operation.id", ondelete="RESTRICT")
    )
    api_client_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("api_client.id", ondelete="RESTRICT"))
    credential_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_client_credential.id", ondelete="RESTRICT")
    )
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(255))
    stream: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    business_code: Mapped[str] = mapped_column(String(64), nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quota_overrun: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    charged_micro: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    qps_observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    concurrency_observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_requests_after: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    daily_tokens_after: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_requests_after: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_tokens_after: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    remote_ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent_family: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)

    operation: Mapped["ApiCallOperation | None"] = relationship(back_populates="terminal_event")
    client: Mapped["ApiClient | None"] = relationship(back_populates="events")
    credential: Mapped["ApiClientCredential | None"] = relationship(back_populates="events")


@event.listens_for(ApiCallEvent, "before_update")
@event.listens_for(ApiCallEvent, "before_delete")
def reject_terminal_event_mutation(*_args: object) -> None:
    raise ImmutableEventError("terminal API call events are append-only")


@event.listens_for(Session, "do_orm_execute")
def reject_terminal_event_bulk_mutation(execute_state: ORMExecuteState) -> None:
    statement = execute_state.statement
    entity = getattr(statement, "entity_description", {}).get("entity")
    if (execute_state.is_update or execute_state.is_delete) and entity is ApiCallEvent:
        raise ImmutableEventError("terminal API call events are append-only")
