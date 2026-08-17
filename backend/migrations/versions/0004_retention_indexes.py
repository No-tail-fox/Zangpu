"""add global retention indexes

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_api_call_event_created", "api_call_event", ["created_at", "id"], unique=False)
    op.create_index(
        "ix_api_client_admin_audit_created",
        "api_client_admin_audit",
        ["created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_api_client_admin_audit_created", table_name="api_client_admin_audit")
    op.drop_index("ix_api_call_event_created", table_name="api_call_event")
