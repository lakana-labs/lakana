"""A table's health effects, and the health signal a notification delivery is about.

Revision ID: d6f0a2b4c8e9
Revises: c5e9f1a3b7d8
Create Date: 2026-11-05 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6f0a2b4c8e9"
down_revision: str | Sequence[str] | None = "c5e9f1a3b7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Two nullable columns: a table's effects arrive with its next deploy; a delivery of a health
    signal names it.
    """
    with op.batch_alter_table("tables") as batch_op:
        batch_op.add_column(sa.Column("health_effects", sa.JSON(), nullable=True))
    with op.batch_alter_table("notification_deliveries") as batch_op:
        batch_op.add_column(sa.Column("health_signal_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_notification_deliveries_health_signal_id_table_health_signals"),
            "table_health_signals",
            ["health_signal_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            op.f("uq_notification_deliveries_health_signal_id_job"), ["health_signal_id", "job"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("notification_deliveries") as batch_op:
        batch_op.drop_constraint(
            op.f("uq_notification_deliveries_health_signal_id_job"), type_="unique"
        )
        batch_op.drop_constraint(
            op.f("fk_notification_deliveries_health_signal_id_table_health_signals"),
            type_="foreignkey",
        )
        batch_op.drop_column("health_signal_id")
    with op.batch_alter_table("tables") as batch_op:
        batch_op.drop_column("health_effects")
