from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import JSON, BigInteger, CheckConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, Session, mapped_column

from backend.app.models.base import Base, new_uuid, utc_epoch_seconds
from backend.app.models.bindings import ApiClientBinding

OUTBOX_TARGETS = ("bifrost", "openwebui")
OUTBOX_ACTIONS = ("create", "update", "disable", "compensate")
OUTBOX_STATUSES = ("pending", "processing", "completed", "failed")
SENSITIVE_PAYLOAD_FRAGMENTS = (
    "answer",
    "authorization",
    "bifrostvalue",
    "ciphertext",
    "nonce",
    "prompt",
    "rawbody",
    "rawrequest",
    "rawresponse",
    "secret",
    "signature",
    "upstreamerror",
    "xbfvk",
)


class ControlOutbox(Base):
    __tablename__ = "control_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_control_outbox_idempotency_key"),
        CheckConstraint(f"target IN {OUTBOX_TARGETS!r}", name="target_values"),
        CheckConstraint(f"action IN {OUTBOX_ACTIONS!r}", name="action_values"),
        CheckConstraint(f"status IN {OUTBOX_STATUSES!r}", name="status_values"),
        CheckConstraint("attempt_count BETWEEN 0 AND 1000", name="attempt_count_bounds"),
        CheckConstraint("last_error_code IS NULL OR length(last_error_code) <= 64", name="error_code_length"),
        Index("ix_control_outbox_aggregate", "aggregate_type", "aggregate_id"),
        Index("ix_control_outbox_target_status_available", "target", "status", "available_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)
    locked_at: Mapped[int | None] = mapped_column(BigInteger)
    completed_at: Mapped[int | None] = mapped_column(BigInteger)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)


def payload_contains_sensitive_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            not isinstance(key, str)
            or any(
                fragment in "".join(character.lower() for character in key if character.isalnum())
                for fragment in SENSITIVE_PAYLOAD_FRAGMENTS
            )
            or payload_contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return any(payload_contains_sensitive_key(item) for item in value)
    return False


def queue_binding_sync(
    session: Session,
    *,
    binding: ApiClientBinding,
    desired_config_hash: str,
    target: str,
    action: str,
    idempotency_key: str,
    payload: dict[str, Any],
    now: int | None = None,
) -> ControlOutbox:
    if payload_contains_sensitive_key(payload):
        raise ValueError("outbox payload contains a sensitive key")
    if target not in OUTBOX_TARGETS or action not in OUTBOX_ACTIONS:
        raise ValueError("unsupported outbox target or action")

    changed_at = utc_epoch_seconds() if now is None else now
    binding.bifrost_config_hash = desired_config_hash
    binding.sync_status = "pending"
    binding.last_sync_error_code = None
    binding.version += 1
    binding.updated_at = changed_at

    item = ControlOutbox(
        aggregate_type="api_client_binding",
        aggregate_id=binding.id,
        target=target,
        action=action,
        idempotency_key=idempotency_key,
        payload=payload,
        status="pending",
        attempt_count=0,
        available_at=changed_at,
        created_at=changed_at,
        updated_at=changed_at,
    )
    session.add(item)
    return item
