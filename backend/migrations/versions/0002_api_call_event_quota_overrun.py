"""persist quota overrun evidence

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("api_call_event", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "quota_overrun",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )

    with op.batch_alter_table("api_call_event", schema=None) as batch_op:
        batch_op.alter_column("quota_overrun", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("api_call_event", schema=None) as batch_op:
        batch_op.drop_column("quota_overrun")
