from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, new_uuid, utc_epoch_seconds

if TYPE_CHECKING:
    from backend.app.models.clients import ApiClient
    from backend.app.models.events import ApiCallEvent
    from backend.app.models.operations import ApiCallOperation


CREDENTIAL_STATUSES = ("active", "revoked")


class ApiClientCredential(Base):
    __tablename__ = "api_client_credential"
    __table_args__ = (
        UniqueConstraint("key_id", name="uq_api_client_credential_key_id"),
        CheckConstraint("length(key_id) BETWEEN 8 AND 80", name="key_id_length"),
        CheckConstraint("substr(key_id, 1, 4) = 'zpk_'", name="key_id_prefix"),
        CheckConstraint(f"status IN {CREDENTIAL_STATUSES!r}", name="status_values"),
        CheckConstraint("expires_at IS NULL OR expires_at > 0", name="expires_at_positive"),
        Index("ix_api_client_credential_api_client_id", "api_client_id"),
        Index("ix_api_client_credential_replaced_by_id", "replaced_by_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    api_client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_client.id", ondelete="RESTRICT"), nullable=False
    )
    key_id: Mapped[str] = mapped_column(String(80), nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    secret_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    master_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expires_at: Mapped[int | None] = mapped_column(BigInteger)
    last_used_at: Mapped[int | None] = mapped_column(BigInteger)
    replaced_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_client_credential.id", ondelete="RESTRICT")
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    revoked_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger)

    client: Mapped["ApiClient"] = relationship(back_populates="credentials")
    replaced_by: Mapped["ApiClientCredential | None"] = relationship(remote_side="ApiClientCredential.id")
    operations: Mapped[list["ApiCallOperation"]] = relationship(back_populates="credential")
    events: Mapped[list["ApiCallEvent"]] = relationship(back_populates="credential")


class CredentialSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str
    api_client_id: str
    key_id: str
    status: str
    expires_at: int | None
    last_used_at: int | None
    replaced_by_id: str | None
    created_at: int
    revoked_at: int | None
