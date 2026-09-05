"""P2 audit baseline owned by the migration role."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260904_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("entity", sa.String(length=255), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        op.f("ix_audit_events_occurred_at"),
        "audit_events",
        ["occurred_at"],
        unique=False,
    )
    op.execute("REVOKE ALL ON TABLE audit_events FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT ON TABLE audit_events TO directpilot_app")


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_events_occurred_at"), table_name="audit_events")
    op.drop_table("audit_events")
