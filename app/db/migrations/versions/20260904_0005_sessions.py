"""P3 opaque server-side session storage."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260904_0005"
down_revision = "20260904_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("active_workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("idle_expires_at <= expires_at", name="session_idle_before_absolute"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_sessions_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["active_workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_sessions_active_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"])
    op.create_index(
        op.f("ix_sessions_active_token_hash"),
        "sessions",
        ["token_hash"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.execute("REVOKE ALL ON TABLE sessions FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE sessions TO directpilot_app")


def downgrade() -> None:
    op.drop_index(op.f("ix_sessions_active_token_hash"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_table("sessions")
