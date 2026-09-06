"""P4 encrypted workspace-scoped Yandex provider connections."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260906_0008"
down_revision = "20260905_0007"
branch_labels = None
depends_on = None

_WORKSPACE_SETTING = "NULLIF(current_setting('app.current_workspace_id', true), '')::uuid"
_USER_SETTING = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"
_MEMBERSHIP_CHECK = f"public.directpilot_has_active_membership({_WORKSPACE_SETTING}, {_USER_SETTING})"


def _enable_provider_connection_rls() -> None:
    identity_owner_check = (
        "EXISTS (SELECT 1 FROM external_identities "
        "WHERE external_identities.id = yandex_provider_connections.external_identity_id "
        f"AND external_identities.user_id = {_USER_SETTING})"
    )
    predicate = (
        f"workspace_id = {_WORKSPACE_SETTING} AND {_MEMBERSHIP_CHECK} "
        f"AND {identity_owner_check}"
    )
    op.execute("ALTER TABLE yandex_provider_connections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE yandex_provider_connections FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY yandex_provider_connections_workspace_isolation "
        "ON yandex_provider_connections FOR ALL TO directpilot_app "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    op.create_table(
        "yandex_provider_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("token_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("wrap_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("kek_key_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credential_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "provider = 'yandex'",
            name=op.f("ck_yandex_provider_connections_provider_yandex"),
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name=op.f("ck_yandex_provider_connections_schema_version_positive"),
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_yandex_provider_connections_version_positive"),
        ),
        sa.CheckConstraint(
            "octet_length(token_nonce) = 12",
            name=op.f("ck_yandex_provider_connections_token_nonce_length"),
        ),
        sa.CheckConstraint(
            "octet_length(wrap_nonce) = 12",
            name=op.f("ck_yandex_provider_connections_wrap_nonce_length"),
        ),
        sa.CheckConstraint(
            "char_length(kek_key_id) > 0",
            name=op.f("ck_yandex_provider_connections_kek_key_id_not_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_yandex_provider_connections_workspace_id_workspaces"),
        ),
        sa.ForeignKeyConstraint(
            ["external_identity_id"],
            ["external_identities.id"],
            name=op.f(
                "fk_yandex_provider_connections_external_identity_id_external_identities"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_yandex_provider_connections")),
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "external_identity_id",
            name=op.f("uq_yandex_provider_connections_workspace_id"),
        ),
    )
    op.create_index(
        op.f("ix_yandex_provider_connections_workspace_id"),
        "yandex_provider_connections",
        ["workspace_id"],
    )
    op.create_index(
        op.f("ix_yandex_provider_connections_external_identity_id"),
        "yandex_provider_connections",
        ["external_identity_id"],
    )
    op.create_index(
        op.f("ix_yandex_provider_connections_status"),
        "yandex_provider_connections",
        ["status"],
    )
    op.execute("REVOKE ALL ON TABLE yandex_provider_connections FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE yandex_provider_connections TO directpilot_app")
    _enable_provider_connection_rls()


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS yandex_provider_connections_workspace_isolation "
        "ON yandex_provider_connections"
    )
    op.execute("ALTER TABLE yandex_provider_connections NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE yandex_provider_connections DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        op.f("ix_yandex_provider_connections_status"),
        table_name="yandex_provider_connections",
    )
    op.drop_index(
        op.f("ix_yandex_provider_connections_external_identity_id"),
        table_name="yandex_provider_connections",
    )
    op.drop_index(
        op.f("ix_yandex_provider_connections_workspace_id"),
        table_name="yandex_provider_connections",
    )
    op.drop_table("yandex_provider_connections")
