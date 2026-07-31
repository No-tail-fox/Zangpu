from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, new_uuid, utc_epoch_seconds

if TYPE_CHECKING:
    from backend.app.models.clients import ApiClient
    from backend.app.models.credentials import ApiClientCredential
    from backend.app.models.events import ApiCallEvent


OPERATION_STATUSES = ("pending", "completed", "rejected", "abandoned")


class ApiCallOperation(Base):
    __tablename__ = "api_call_operation"
    __table_args__ = (
        UniqueConstraint("api_client_id", "client_request_id", name="uq_api_call_operation_client_request"),
        CheckConstraint(f"status IN {OPERATION_STATUSES!r}", name="status_values"),
        CheckConstraint("reserved_tokens >= 0", name="reserved_tokens_nonnegative"),
        CheckConstraint("prompt_tokens >= 0", name="prompt_tokens_nonnegative"),
        CheckConstraint("completion_tokens >= 0", name="completion_tokens_nonnegative"),
        CheckConstraint("total_tokens >= 0", name="total_tokens_nonnegative"),
        CheckConstraint("total_tokens = prompt_tokens + completion_tokens", name="token_total_matches"),
        CheckConstraint("terminal_http_status IS NULL OR terminal_http_status BETWEEN 100 AND 599", name="http_status"),
        CheckConstraint("completed_at IS NULL OR completed_at >= started_at", name="completion_order"),
        Index("ix_api_call_operation_credential_id", "credential_id"),
        Index("ix_api_call_operation_status_updated", "status", "updated_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    api_client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_client.id", ondelete="RESTRICT"), nullable=False
    )
    credential_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_client_credential.id", ondelete="RESTRICT"), nullable=False
    )
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    credit_settlement_id: Mapped[str | None] = mapped_column(String(128))
    usage_operation_id: Mapped[str | None] = mapped_column(String(128))
    terminal_http_status: Mapped[int | None] = mapped_column(Integer)
    terminal_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)
    completed_at: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)

    client: Mapped["ApiClient"] = relationship(back_populates="operations")
    credential: Mapped["ApiClientCredential"] = relationship(back_populates="operations")
    terminal_event: Mapped["ApiCallEvent | None"] = relationship(back_populates="operation", uselist=False)
