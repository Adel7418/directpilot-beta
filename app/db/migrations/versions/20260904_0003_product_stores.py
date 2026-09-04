"""P2 durable product drafts and semantic packages."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260904_0003"
down_revision = "20260904_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_drafts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_drafts")),
    )
    op.create_table(
        "semantic_change_packages",
        sa.Column("package_id", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("package_id", name=op.f("pk_semantic_change_packages")),
    )
    op.execute("REVOKE ALL ON TABLE campaign_drafts FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE semantic_change_packages FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE campaign_drafts TO directpilot_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE semantic_change_packages TO directpilot_app"
    )


def downgrade() -> None:
    op.drop_table("semantic_change_packages")
    op.drop_table("campaign_drafts")
