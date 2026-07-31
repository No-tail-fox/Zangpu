from collections.abc import Mapping
from math import isfinite
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, BigInteger, CheckConstraint, ForeignKey, Index, String, event
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column, relationship, validates

from backend.app.models.base import Base, new_uuid, utc_epoch_seconds, validate_string_set

if TYPE_CHECKING:
    from backend.app.models.clients import ApiClient


AUDIT_TARGET_TYPES = ("client", "credential", "permission", "quota", "group", "status", "retention", "export")
SENSITIVE_AUDIT_TOKENS = {
    "authorization",
    "body",
    "ciphertext",
    "nonce",
    "prompt",
    "query",
    "secret",
    "signature",
}
SENSITIVE_AUDIT_FRAGMENTS = (
    "answer",
    "authorization",
    "bifrost_value",
    "body",
    "ciphertext",
    "nonce",
    "prompt",
    "query",
    "raw_error",
    "raw_response",
    "secret",
    "signature",
    "upstream_error",
    "x_bf_vk",
)
AUDIT_MAX_FIELDS = 64
AUDIT_MAX_LIST_ITEMS = 256
AUDIT_MAX_VALUE_LENGTH = 512


class ImmutableAuditError(RuntimeError):
    pass


def _normalized_field_name(field_name: str) -> str:
    return "_".join("".join(character.lower() if character.isalnum() else " " for character in field_name).split())


def audit_field_is_sensitive(field_name: str) -> bool:
    normalized = _normalized_field_name(field_name)
    tokens = set(normalized.split("_"))
    return bool(tokens & SENSITIVE_AUDIT_TOKENS) or any(
        fragment in normalized for fragment in SENSITIVE_AUDIT_FRAGMENTS
    )


def _validate_summary_scalar(field_name: str, value: object) -> object:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{field_name} contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > AUDIT_MAX_VALUE_LENGTH:
            raise ValueError(f"{field_name} contains an oversized value")
        return value
    raise ValueError(f"{field_name} must contain only flat JSON scalar values or scalar lists")


def _validate_summary_value(field_name: str, value: object) -> object:
    if isinstance(value, list):
        if len(value) > AUDIT_MAX_LIST_ITEMS:
            raise ValueError(f"{field_name} contains an oversized list")
        return [_validate_summary_scalar(field_name, item) for item in value]
    return _validate_summary_scalar(field_name, value)


def validate_audit_summary(field_name: str, value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or len(value) > AUDIT_MAX_FIELDS:
        raise ValueError(f"{field_name} must be a bounded object")

    summary: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ValueError(f"{field_name} contains an invalid field name")
        if audit_field_is_sensitive(key):
            raise ValueError(f"{field_name} contains a sensitive field")
        summary[key] = _validate_summary_value(field_name, item)
    return {key: summary[key] for key in sorted(summary)}


class ApiClientAdminAudit(Base):
    __tablename__ = "api_client_admin_audit"
    __table_args__ = (
        CheckConstraint(f"target_type IN {AUDIT_TARGET_TYPES!r}", name="target_type_values"),
        CheckConstraint("length(actor_user_id) BETWEEN 1 AND 128", name="actor_user_id_length"),
        CheckConstraint("target_id IS NULL OR length(target_id) BETWEEN 1 AND 128", name="target_id_length"),
        CheckConstraint("length(action) BETWEEN 1 AND 64", name="action_length"),
        Index("ix_api_client_admin_audit_api_client_created", "api_client_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    actor_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    api_client_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("api_client.id", ondelete="RESTRICT"))
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    before_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    after_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=utc_epoch_seconds)

    client: Mapped["ApiClient | None"] = relationship(back_populates="admin_audits")

    @validates("target_type")
    def validate_target_type(self, _key: str, value: str) -> str:
        if value not in AUDIT_TARGET_TYPES:
            raise ValueError("unsupported audit target type")
        return value

    @validates("action")
    def validate_action(self, _key: str, value: str) -> str:
        if not value or len(value) > 64:
            raise ValueError("audit action must be between 1 and 64 characters")
        return value

    @validates("changed_fields")
    def validate_changed_fields(self, _key: str, value: object) -> list[str]:
        fields = validate_string_set("changed_fields", value, max_items=AUDIT_MAX_FIELDS, max_item_length=64)
        if any(audit_field_is_sensitive(field) for field in fields):
            raise ValueError("changed_fields contains a sensitive field")
        return fields

    @validates("before_summary", "after_summary")
    def validate_summaries(self, key: str, value: object) -> dict[str, Any]:
        return validate_audit_summary(key, value)


@event.listens_for(ApiClientAdminAudit, "before_insert")
def validate_audit_field_alignment(_mapper: object, _connection: object, audit: ApiClientAdminAudit) -> None:
    changed_fields = set(audit.changed_fields)
    summary_fields = set(audit.before_summary) | set(audit.after_summary)
    if not summary_fields.issubset(changed_fields):
        raise ValueError("audit summary fields must be included in changed_fields")


@event.listens_for(ApiClientAdminAudit, "before_update")
@event.listens_for(ApiClientAdminAudit, "before_delete")
def reject_admin_audit_mutation(*_args: object) -> None:
    raise ImmutableAuditError("admin audit records are append-only")


@event.listens_for(Session, "do_orm_execute")
def reject_admin_audit_bulk_mutation(execute_state: ORMExecuteState) -> None:
    statement = execute_state.statement
    entity = getattr(statement, "entity_description", {}).get("entity")
    if (execute_state.is_update or execute_state.is_delete) and entity is ApiClientAdminAudit:
        raise ImmutableAuditError("admin audit records are append-only")
