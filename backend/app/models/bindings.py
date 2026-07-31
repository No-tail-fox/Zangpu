from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, new_uuid, utc_epoch_seconds

if TYPE_CHECKING:
    from backend.app.models.clients import ApiClient


BINDING_SYNC_STATUSES = ("pending", "active", "disabled", "error")


class ApiClientBinding(Base):
    __tablename__ = "api_client_binding"
    __table_args__ = (
        UniqueConstraint("api_client_id", name="uq_api_client_binding_api_client_id"),
        UniqueConstraint("zangpu_service_user_id", name="uq_api_client_binding_service_user_id"),
        UniqueConstraint("bifrost_virtual_key_id", name="uq_api_client_binding_bifrost_key_id"),
        CheckConstraint(f"sync_status IN {BINDING_SYNC_STATUSES!r}", name="sync_status_values"),
        CheckConstraint(
            "sync_status NOT IN ('active', 'disabled') OR "
            "(zangpu_service_user_id IS NOT NULL AND bifrost_virtual_key_id IS NOT NULL AND "
            "bifrost_value_ciphertext IS NOT NULL AND bifrost_value_key_version IS NOT NULL)",
            name="synced_remote_fields",
        ),
        CheckConstraint("last_sync_error_code IS NULL OR length(last_sync_error_code) <= 64", name="error_code_length"),
        CheckConstraint("version > 0", name="version_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    api_client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_client.id", ondelete="RESTRICT"), nullable=False
    )
    zangpu_service_user_id: Mapped[str | None] = mapped_column(String(128))
    bifrost_virtual_key_id: Mapped[str | None] = mapped_column(String(128))
    bifrost_value_ciphertext: Mapped[str | None] = mapped_column(Text)
    bifrost_value_key_version: Mapped[str | None] = mapped_column(String(64))
    bifrost_config_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    sync_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    last_sync_error_code: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)

    client: Mapped["ApiClient"] = relationship(back_populates="binding")


class BindingSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str
    api_client_id: str
    zangpu_service_user_id: str | None
    bifrost_virtual_key_id: str | None
    bifrost_config_hash: str
    sync_status: str
    last_sync_error_code: str | None
    version: int
    created_at: int
    updated_at: int
