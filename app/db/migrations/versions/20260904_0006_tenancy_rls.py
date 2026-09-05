"""P3 workspace tenancy, legacy backfill, and PostgreSQL RLS."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260904_0006"
down_revision = "20260904_0005"
branch_labels = None
depends_on = None

_LEGACY_USER_ID = "00000000-0000-4000-8000-000000000301"
_LEGACY_WORKSPACE_ID = "00000000-0000-4000-8000-000000000302"
_LEGACY_MEMBERSHIP_ID = "00000000-0000-4000-8000-000000000303"
_WORKSPACE_SETTING = "NULLIF(current_setting('app.current_workspace_id', true), '')::uuid"
_USER_SETTING = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"
_MEMBERSHIP_CHECK = f"public.directpilot_has_active_membership({_WORKSPACE_SETTING}, {_USER_SETTING})"


def _create_legacy_operator_workspace() -> None:
    op.execute(
        f"""
        INSERT INTO users (id, yandex_subject, display_name, status, created_at, updated_at)
        VALUES ('{_LEGACY_USER_ID}', 'legacy-operator', 'Legacy operator', 'active',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO workspaces (id, name, kind, status, created_by_user_id, created_at, updated_at)
        VALUES ('{_LEGACY_WORKSPACE_ID}', 'Legacy operator workspace', 'personal', 'active',
                '{_LEGACY_USER_ID}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO memberships (id, workspace_id, user_id, role, status, created_at, accepted_at)
        VALUES ('{_LEGACY_MEMBERSHIP_ID}', '{_LEGACY_WORKSPACE_ID}', '{_LEGACY_USER_ID}',
                'owner', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (workspace_id, user_id) DO NOTHING
        """
    )


def _add_workspace_column(table_name: str, *, nullable: bool) -> None:
    op.add_column(
        table_name,
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        f"UPDATE {table_name} SET workspace_id = '{_LEGACY_WORKSPACE_ID}' "
        "WHERE workspace_id IS NULL"
    )
    if not nullable:
        op.alter_column(table_name, "workspace_id", nullable=False)
    op.create_foreign_key(
        op.f(f"fk_{table_name}_workspace_id_workspaces"),
        table_name,
        "workspaces",
        ["workspace_id"],
        ["id"],
    )
    op.create_index(op.f(f"ix_{table_name}_workspace_id"), table_name, ["workspace_id"])


def _create_membership_helper() -> None:
    op.execute("GRANT USAGE ON SCHEMA public TO directpilot_rls_helper")
    op.execute("GRANT SELECT ON TABLE memberships TO directpilot_rls_helper")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.directpilot_has_active_membership(
            workspace_id uuid,
            user_id uuid
        ) RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT EXISTS (
                SELECT 1
                FROM public.memberships AS m
                WHERE m.workspace_id = $1
                  AND m.user_id = $2
                  AND m.status = 'active'
            )
        $$
        """
    )
    op.execute("ALTER FUNCTION public.directpilot_has_active_membership(uuid, uuid) OWNER TO directpilot_rls_helper")
    op.execute("REVOKE ALL ON FUNCTION public.directpilot_has_active_membership(uuid, uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.directpilot_has_active_membership(uuid, uuid) TO directpilot_app")


def _bootstrap_workspace_check() -> str:
    return (
        f"id = {_WORKSPACE_SETTING} AND created_by_user_id = {_USER_SETTING} AND "
        "NOT public.directpilot_workspace_has_memberships(id)"
    )


def _bootstrap_membership_check() -> str:
    return (
        f"workspace_id = {_WORKSPACE_SETTING} AND user_id = {_USER_SETTING} AND "
        "role = 'owner' AND status = 'active' AND "
        "public.directpilot_workspace_owned_by(workspace_id, user_id) AND "
        "NOT public.directpilot_workspace_has_memberships(workspace_id)"
    )


def _create_bootstrap_helpers() -> None:
    op.execute("GRANT SELECT ON TABLE workspaces TO directpilot_rls_helper")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.directpilot_workspace_has_memberships(
            workspace_id uuid
        ) RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT EXISTS (
                SELECT 1
                FROM public.memberships AS m
                WHERE m.workspace_id = $1
            )
        $$
        """
    )
    op.execute("ALTER FUNCTION public.directpilot_workspace_has_memberships(uuid) OWNER TO directpilot_rls_helper")
    op.execute("REVOKE ALL ON FUNCTION public.directpilot_workspace_has_memberships(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.directpilot_workspace_has_memberships(uuid) TO directpilot_app")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.directpilot_workspace_owned_by(
            workspace_id uuid,
            user_id uuid
        ) RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT EXISTS (
                SELECT 1
                FROM public.workspaces AS w
                WHERE w.id = $1
                  AND w.created_by_user_id = $2
            )
        $$
        """
    )
    op.execute("ALTER FUNCTION public.directpilot_workspace_owned_by(uuid, uuid) OWNER TO directpilot_rls_helper")
    op.execute("REVOKE ALL ON FUNCTION public.directpilot_workspace_owned_by(uuid, uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.directpilot_workspace_owned_by(uuid, uuid) TO directpilot_app")


def _enable_workspace_rls(
    table_name: str,
    *,
    tenant_column: str = "workspace_id",
    platform_auth_events: bool = False,
    extra_check: str | None = None,
) -> None:
    workspace_clause = f"{tenant_column} = {_WORKSPACE_SETTING}"
    using_clause = f"{workspace_clause} AND {_MEMBERSHIP_CHECK}"
    check_clause = using_clause if extra_check is None else f"({using_clause}) OR ({extra_check})"
    if platform_auth_events:
        check_clause = (
            f"({using_clause}) OR "
            "(workspace_id IS NULL AND action LIKE 'platform_auth.%')"
        )
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table_name}_workspace_isolation ON {table_name} "
        f"FOR ALL TO directpilot_app USING ({using_clause}) WITH CHECK ({check_clause})"
    )


def upgrade() -> None:
    _create_legacy_operator_workspace()
    _create_membership_helper()
    _create_bootstrap_helpers()

    _add_workspace_column("audit_events", nullable=True)
    op.create_check_constraint(
        "audit_workspace_or_platform_auth",
        "audit_events",
        "workspace_id IS NOT NULL OR action LIKE 'platform_auth.%'",
    )

    _add_workspace_column("idempotency_records", nullable=False)
    op.drop_constraint(
        "uq_idempotency_records_namespace",
        "idempotency_records",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_idempotency_records_workspace_namespace_key",
        "idempotency_records",
        ["workspace_id", "namespace", "key_hash"],
    )

    _add_workspace_column("campaign_drafts", nullable=False)
    op.drop_constraint("pk_campaign_drafts", "campaign_drafts", type_="primary")
    op.create_primary_key(
        "pk_campaign_drafts",
        "campaign_drafts",
        ["id", "workspace_id"],
    )

    _add_workspace_column("semantic_change_packages", nullable=False)
    op.drop_constraint(
        "pk_semantic_change_packages",
        "semantic_change_packages",
        type_="primary",
    )
    op.create_primary_key(
        "pk_semantic_change_packages",
        "semantic_change_packages",
        ["package_id", "workspace_id"],
    )

    _enable_workspace_rls("workspaces", tenant_column="id", extra_check=_bootstrap_workspace_check())
    _enable_workspace_rls("memberships", extra_check=_bootstrap_membership_check())
    _enable_workspace_rls("audit_events", platform_auth_events=True)
    _enable_workspace_rls("idempotency_records")
    _enable_workspace_rls("campaign_drafts")
    _enable_workspace_rls("semantic_change_packages")


def downgrade() -> None:
    for table_name in (
        "semantic_change_packages",
        "campaign_drafts",
        "idempotency_records",
        "audit_events",
        "memberships",
        "workspaces",
    ):
        op.execute(f"DROP POLICY IF EXISTS {table_name}_workspace_isolation ON {table_name}")
        op.execute(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP FUNCTION IF EXISTS public.directpilot_workspace_owned_by(uuid, uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.directpilot_workspace_has_memberships(uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.directpilot_has_active_membership(uuid, uuid)")

    op.drop_constraint(
        "pk_semantic_change_packages",
        "semantic_change_packages",
        type_="primary",
    )
    op.create_primary_key(
        "pk_semantic_change_packages",
        "semantic_change_packages",
        ["package_id"],
    )
    op.drop_index(op.f("ix_semantic_change_packages_workspace_id"), table_name="semantic_change_packages")
    op.drop_constraint(
        op.f("fk_semantic_change_packages_workspace_id_workspaces"),
        "semantic_change_packages",
        type_="foreignkey",
    )
    op.drop_column("semantic_change_packages", "workspace_id")

    op.drop_constraint("pk_campaign_drafts", "campaign_drafts", type_="primary")
    op.create_primary_key("pk_campaign_drafts", "campaign_drafts", ["id"])
    op.drop_index(op.f("ix_campaign_drafts_workspace_id"), table_name="campaign_drafts")
    op.drop_constraint(
        op.f("fk_campaign_drafts_workspace_id_workspaces"),
        "campaign_drafts",
        type_="foreignkey",
    )
    op.drop_column("campaign_drafts", "workspace_id")

    op.drop_constraint(
        "uq_idempotency_records_workspace_namespace_key",
        "idempotency_records",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_idempotency_records_namespace",
        "idempotency_records",
        ["namespace", "key_hash"],
    )
    op.drop_index(op.f("ix_idempotency_records_workspace_id"), table_name="idempotency_records")
    op.drop_constraint(
        op.f("fk_idempotency_records_workspace_id_workspaces"),
        "idempotency_records",
        type_="foreignkey",
    )
    op.drop_column("idempotency_records", "workspace_id")

    op.drop_constraint(
        op.f("ck_audit_events_audit_workspace_or_platform_auth"),
        "audit_events",
        type_="check",
    )
    op.drop_index(op.f("ix_audit_events_workspace_id"), table_name="audit_events")
    op.drop_constraint(
        op.f("fk_audit_events_workspace_id_workspaces"),
        "audit_events",
        type_="foreignkey",
    )
    op.drop_column("audit_events", "workspace_id")
