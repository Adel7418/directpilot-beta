"""P4 protected, workspace-scoped Yandex provider accounts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260906_0010"
down_revision = "20260906_0009"
branch_labels = None
depends_on = None

_WORKSPACE_SETTING = "NULLIF(current_setting('app.current_workspace_id', true), '')::uuid"
_USER_SETTING = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"
_MEMBERSHIP_CHECK = f"public.directpilot_has_active_membership({_WORKSPACE_SETTING}, {_USER_SETTING})"


def _enable_provider_account_rls() -> None:
    connection_scope = (
        "EXISTS (SELECT 1 FROM yandex_provider_connections "
        "WHERE yandex_provider_connections.id = provider_accounts.connection_id "
        "AND yandex_provider_connections.workspace_id = provider_accounts.workspace_id "
        "AND yandex_provider_connections.status = 'active')"
    )
    predicate = (
        f"workspace_id = {_WORKSPACE_SETTING} AND {_MEMBERSHIP_CHECK} "
        f"AND {connection_scope}"
    )
    op.execute("ALTER TABLE provider_accounts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE provider_accounts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY provider_accounts_workspace_isolation "
        "ON provider_accounts FOR ALL TO directpilot_app "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    op.create_unique_constraint(
        op.f("uq_yandex_provider_connections_id"),
        "yandex_provider_connections",
        ["id", "workspace_id"],
    )
    op.create_table(
        "provider_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_account_key", sa.String(length=255), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("country_id", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("login_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("login_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("login_wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("login_wrap_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("login_kek_key_id", sa.String(length=128), nullable=False),
        sa.Column("login_schema_version", sa.Integer(), nullable=False),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "account_type IN ('advertiser', 'agency_client')",
            name=op.f("ck_provider_accounts_account_type_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived', 'stale')",
            name=op.f("ck_provider_accounts_status_valid"),
        ),
        sa.CheckConstraint(
            "capabilities = '[\"direct.read\"]'::jsonb",
            name=op.f("ck_provider_accounts_capabilities_read_only"),
        ),
        sa.CheckConstraint(
            "login_schema_version > 0",
            name=op.f("ck_provider_accounts_login_schema_version_positive"),
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_provider_accounts_version_positive"),
        ),
        sa.CheckConstraint(
            "octet_length(login_nonce) = 12",
            name=op.f("ck_provider_accounts_login_nonce_length"),
        ),
        sa.CheckConstraint(
            "octet_length(login_wrap_nonce) = 12",
            name=op.f("ck_provider_accounts_login_wrap_nonce_length"),
        ),
        sa.CheckConstraint(
            "char_length(login_kek_key_id) > 0",
            name=op.f("ck_provider_accounts_login_kek_key_id_not_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_provider_accounts_workspace_id_workspaces"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "workspace_id"],
            ["yandex_provider_connections.id", "yandex_provider_connections.workspace_id"],
            name=op.f("fk_provider_accounts_connection_id_yandex_provider_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_accounts")),
        sa.UniqueConstraint(
            "connection_id",
            "provider_account_key",
            name=op.f("uq_provider_accounts_connection_id"),
        ),
    )
    op.create_index(
        op.f("ix_provider_accounts_workspace_id"),
        "provider_accounts",
        ["workspace_id"],
    )
    op.create_index(
        op.f("ix_provider_accounts_connection_id"),
        "provider_accounts",
        ["connection_id"],
    )
    op.execute("REVOKE ALL ON TABLE provider_accounts FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE provider_accounts TO directpilot_app")
    _enable_provider_account_rls()


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS provider_accounts_workspace_isolation ON provider_accounts")
    op.execute("ALTER TABLE provider_accounts NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE provider_accounts DISABLE ROW LEVEL SECURITY")
    op.drop_index(op.f("ix_provider_accounts_connection_id"), table_name="provider_accounts")
    op.drop_index(op.f("ix_provider_accounts_workspace_id"), table_name="provider_accounts")
    op.drop_table("provider_accounts")
    op.drop_constraint(
        op.f("uq_yandex_provider_connections_id"),
        "yandex_provider_connections",
        type_="unique",
    )
