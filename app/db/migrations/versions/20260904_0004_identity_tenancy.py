"""P3 identity and workspace membership foundation."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260904_0004"
down_revision = "20260904_0003"
branch_labels = None
depends_on = None


_USER_STATUSES = "status IN ('active', 'suspended', 'deleted')"
_WORKSPACE_KINDS = "kind IN ('personal', 'team')"
_WORKSPACE_STATUSES = "status IN ('active', 'suspended', 'deleted')"
_MEMBERSHIP_ROLES = "role IN ('viewer', 'operator', 'approver', 'admin', 'owner')"
_MEMBERSHIP_STATUSES = "status IN ('active', 'revoked')"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("yandex_subject", sa.String(length=255), nullable=True),
        sa.Column("email_normalized", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
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
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_USER_STATUSES, name="user_status"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("yandex_subject", name=op.f("uq_users_yandex_subject")),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.CheckConstraint(_WORKSPACE_KINDS, name="workspace_kind"),
        sa.CheckConstraint(_WORKSPACE_STATUSES, name="workspace_status"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_workspaces_created_by_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )
    op.create_table(
        "memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_MEMBERSHIP_ROLES, name="membership_role"),
        sa.CheckConstraint(_MEMBERSHIP_STATUSES, name="membership_status"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_memberships_workspace_id_workspaces"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_memberships_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            name=op.f("uq_memberships_workspace_id"),
        ),
    )
    op.create_index(op.f("ix_memberships_workspace_id"), "memberships", ["workspace_id"])
    op.create_index(op.f("ix_memberships_user_id"), "memberships", ["user_id"])

    for table_name in ("users", "workspaces", "memberships"):
        op.execute(f"REVOKE ALL ON TABLE {table_name} FROM PUBLIC")
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table_name} TO directpilot_app")


def downgrade() -> None:
    op.drop_index(op.f("ix_memberships_user_id"), table_name="memberships")
    op.drop_index(op.f("ix_memberships_workspace_id"), table_name="memberships")
    op.drop_table("memberships")
    op.drop_table("workspaces")
    op.drop_table("users")
