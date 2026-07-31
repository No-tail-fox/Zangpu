from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, BigInteger, CheckConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.app.models.base import Base, new_uuid, utc_epoch_seconds, validate_string_set

if TYPE_CHECKING:
    from backend.app.models.audits import ApiClientAdminAudit
    from backend.app.models.bindings import ApiClientBinding
    from backend.app.models.credentials import ApiClientCredential
    from backend.app.models.events import ApiCallEvent
    from backend.app.models.operations import ApiCallOperation
    from backend.app.models.quotas import ApiClientQuotaUsage


CLIENT_STATUSES = ("active", "disabled", "archived")
ENDPOINT_PERMISSIONS = ("chat.completions", "models.read", "usage.read", "health.read")


class ApiClient(Base):
    __tablename__ = "api_client"
    __table_args__ = (
        UniqueConstraint("name", name="uq_api_client_name"),
        CheckConstraint("length(name) BETWEEN 1 AND 128", name="name_length"),
        CheckConstraint("description IS NULL OR length(description) <= 1024", name="description_length"),
        CheckConstraint(f"status IN {CLIENT_STATUSES!r}", name="status_values"),
        CheckConstraint("qps_limit > 0", name="qps_positive"),
        CheckConstraint("concurrency_limit > 0", name="concurrency_positive"),
        CheckConstraint("daily_request_limit IS NULL OR daily_request_limit > 0", name="daily_requests_positive"),
        CheckConstraint("daily_token_limit IS NULL OR daily_token_limit > 0", name="daily_tokens_positive"),
        CheckConstraint("total_request_limit IS NULL OR total_request_limit > 0", name="total_requests_positive"),
        CheckConstraint("total_token_limit IS NULL OR total_token_limit > 0", name="total_tokens_positive"),
        CheckConstraint("max_output_tokens_per_request > 0", name="max_output_tokens_positive"),
        CheckConstraint("version > 0", name="version_positive"),
        Index("ix_api_client_status_name", "status", "name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    allowed_endpoints: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    allowed_models: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    group_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    qps_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    concurrency_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_request_limit: Mapped[int | None] = mapped_column(BigInteger)
    daily_token_limit: Mapped[int | None] = mapped_column(BigInteger)
    total_request_limit: Mapped[int | None] = mapped_column(BigInteger)
    total_token_limit: Mapped[int | None] = mapped_column(BigInteger)
    max_output_tokens_per_request: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)
    disabled_at: Mapped[int | None] = mapped_column(BigInteger)
    archived_at: Mapped[int | None] = mapped_column(BigInteger)

    credentials: Mapped[list["ApiClientCredential"]] = relationship(back_populates="client")
    quotas: Mapped[list["ApiClientQuotaUsage"]] = relationship(back_populates="client")
    operations: Mapped[list["ApiCallOperation"]] = relationship(back_populates="client")
    events: Mapped[list["ApiCallEvent"]] = relationship(back_populates="client")
    binding: Mapped["ApiClientBinding | None"] = relationship(back_populates="client", uselist=False)
    admin_audits: Mapped[list["ApiClientAdminAudit"]] = relationship(
        back_populates="client", passive_deletes=True
    )

    @validates("allowed_endpoints", "allowed_models", "group_ids")
    def validate_sets(self, key: str, values: object) -> list[str]:
        if key == "allowed_endpoints":
            return validate_string_set(key, values, allowed=ENDPOINT_PERMISSIONS, max_items=len(ENDPOINT_PERMISSIONS))
        return validate_string_set(key, values)


class ClientSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str
    name: str
    description: str | None
    status: str
    allowed_endpoints: list[str]
    allowed_models: list[str]
    group_ids: list[str]
    qps_limit: int
    concurrency_limit: int
    daily_request_limit: int | None
    daily_token_limit: int | None
    total_request_limit: int | None
    total_token_limit: int | None
    max_output_tokens_per_request: int
    version: int
    created_at: int
    updated_at: int
