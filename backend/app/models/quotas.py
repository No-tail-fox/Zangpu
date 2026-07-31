from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, new_uuid, utc_epoch_seconds

if TYPE_CHECKING:
    from backend.app.models.clients import ApiClient


QUOTA_SCOPES = ("daily", "lifetime")


class ApiClientQuotaUsage(Base):
    __tablename__ = "api_client_quota_usage"
    __table_args__ = (
        UniqueConstraint("api_client_id", "scope", "period_start", name="uq_api_client_quota_scope_period"),
        CheckConstraint(f"scope IN {QUOTA_SCOPES!r}", name="scope_values"),
        CheckConstraint(
            "(scope = 'daily' AND period_start > 0) OR (scope = 'lifetime' AND period_start = 0)",
            name="scope_period",
        ),
        CheckConstraint("request_count >= 0", name="request_count_nonnegative"),
        CheckConstraint("token_reserved >= 0", name="token_reserved_nonnegative"),
        CheckConstraint("token_consumed >= 0", name="token_consumed_nonnegative"),
        CheckConstraint("version > 0", name="version_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    api_client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_client.id", ondelete="RESTRICT"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    token_reserved: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    token_consumed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)

    client: Mapped["ApiClient"] = relationship(back_populates="quotas")
