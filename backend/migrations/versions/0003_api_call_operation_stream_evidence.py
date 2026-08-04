"""persist stream and provider usage evidence

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("api_call_operation", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "stream",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "provider_usage_recorded",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )

    with op.batch_alter_table("api_call_operation", schema=None) as batch_op:
        batch_op.alter_column("stream", server_default=None)
        batch_op.alter_column("provider_usage_recorded", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("api_call_operation", schema=None) as batch_op:
        batch_op.drop_column("provider_usage_recorded")
        batch_op.drop_column("stream")
