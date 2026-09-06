"""P4 Yandex OAuth transactions and external identity mapping."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260905_0007"
down_revision = "20260904_0006"
branch_labels = None
depends_on = None

_WORKSPACE_SETTING = "NULLIF(current_setting('app.current_workspace_id', true), '')::uuid"
_USER_SETTING = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"
_MEMBERSHIP_CHECK = f"public.directpilot_has_active_membership({_WORKSPACE_SETTING}, {_USER_SETTING})"


def _enable_oauth_transaction_rls() -> None:
    predicate = (
        f"workspace_id = {_WORKSPACE_SETTING} AND user_id = {_USER_SETTING} "
        f"AND {_MEMBERSHIP_CHECK}"
    )
    op.execute("ALTER TABLE yandex_oauth_transactions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE yandex_oauth_transactions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY yandex_oauth_transactions_workspace_isolation "
        "ON yandex_oauth_transactions FOR ALL TO directpilot_app "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def _enable_external_identity_rls() -> None:
    predicate = f"user_id = {_USER_SETTING}"
    op.execute("ALTER TABLE external_identities ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE external_identities FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY external_identities_user_isolation "
        "ON external_identities FOR ALL TO directpilot_app "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    op.create_table(
        "yandex_oauth_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("browser_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("return_path", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_yandex_oauth_transactions_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_yandex_oauth_transactions_workspace_id_workspaces"),
        ),
        sa.ForeignKeyConstraint(
            ["browser_session_id"],
            ["sessions.id"],
            name=op.f("fk_yandex_oauth_transactions_browser_session_id_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_yandex_oauth_transactions")),
        sa.UniqueConstraint("state_hash", name=op.f("uq_yandex_oauth_transactions_state_hash")),
    )
    op.create_index(
        op.f("ix_yandex_oauth_transactions_expires_at"),
        "yandex_oauth_transactions",
        ["expires_at"],
    )

    op.create_table(
        "external_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_login", sa.String(length=255), nullable=True),
        sa.Column("profile_display_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_external_identities_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_identities")),
        sa.UniqueConstraint("issuer", "subject", name=op.f("uq_external_identities_issuer")),
        sa.UniqueConstraint("user_id", "issuer", name=op.f("uq_external_identities_user_id")),
    )

    for table_name in ("yandex_oauth_transactions", "external_identities"):
        op.execute(f"REVOKE ALL ON TABLE {table_name} FROM PUBLIC")
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table_name} TO directpilot_app")

    _enable_oauth_transaction_rls()
    _enable_external_identity_rls()


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS external_identities_user_isolation ON external_identities"
    )
    op.execute("ALTER TABLE external_identities NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE external_identities DISABLE ROW LEVEL SECURITY")
    op.execute(
        "DROP POLICY IF EXISTS yandex_oauth_transactions_workspace_isolation "
        "ON yandex_oauth_transactions"
    )
    op.execute("ALTER TABLE yandex_oauth_transactions NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE yandex_oauth_transactions DISABLE ROW LEVEL SECURITY")
    op.drop_table("external_identities")
    op.drop_index(
        op.f("ix_yandex_oauth_transactions_expires_at"),
        table_name="yandex_oauth_transactions",
    )
    op.drop_table("yandex_oauth_transactions")
