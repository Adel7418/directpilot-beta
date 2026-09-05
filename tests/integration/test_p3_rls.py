from __future__ import annotations

import pytest
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.models import CampaignDraftRecord, WorkspaceRecord
from app.db.rls import TenantContext, set_local_tenant_context, tenant_transaction
from app.db.roles import bootstrap_database_roles, get_role_attributes, get_table_owner
from app.modules.identity.repository import PostgresIdentityRepository


@pytest.mark.integration
def test_rls_requires_transaction_local_workspace_context_and_cannot_leak_or_be_disabled(
    postgres_service: object,
) -> None:
    owner_runtime = create_database_runtime(
        DatabaseSettings.from_mapping(
            {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "owner_url")}
        )
    )
    app_runtime = create_database_runtime(
        DatabaseSettings.from_mapping(
            {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "app_url")}
        )
    )
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        identity = PostgresIdentityRepository(app_runtime.sessions)
        first = identity.create_personal_workspace(
            display_name="Synthetic RLS first user",
            workspace_name="Synthetic RLS first workspace",
            yandex_subject="synthetic-rls-first-001",
        )
        second = identity.create_personal_workspace(
            display_name="Synthetic RLS second user",
            workspace_name="Synthetic RLS second workspace",
            yandex_subject="synthetic-rls-second-001",
        )

        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=first.workspace.id,
            user_id=first.user.id,
        ) as session:
            session.add(
                CampaignDraftRecord(
                    id="p3-rls-first-draft",
                    workspace_id=first.workspace.id,
                    payload={"kind": "synthetic"},
                )
            )

        with app_runtime.sessions() as session:
            with session.begin():
                assert session.scalars(select(CampaignDraftRecord)).all() == []
                assert session.scalars(select(WorkspaceRecord)).all() == []

        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=second.workspace.id,
            user_id=second.user.id,
        ) as session:
            assert (
                session.scalar(
                    select(CampaignDraftRecord).where(
                        CampaignDraftRecord.id == "p3-rls-first-draft",
                        CampaignDraftRecord.workspace_id == second.workspace.id,
                    )
                )
                is None
            )

        with app_runtime.sessions() as session:
            with session.begin():
                set_local_tenant_context(
                    session,
                    TenantContext(workspace_id=first.workspace.id, user_id=first.user.id),
                )
                assert (
                    session.execute(
                        text("SELECT current_setting('app.current_workspace_id', true)")
                    ).scalar_one()
                    == str(first.workspace.id)
                )
            with session.begin():
                assert (
                    session.execute(
                        text("SELECT current_setting('app.current_workspace_id', true)")
                    ).scalar_one()
                    in (None, "")
                )

        with app_runtime.engine.begin() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(text("ALTER TABLE campaign_drafts DISABLE ROW LEVEL SECURITY"))

        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=second.workspace.id,
            user_id=second.user.id,
        ) as session:
            session.add(
                CampaignDraftRecord(
                    id="p3-rls-second-draft",
                    workspace_id=second.workspace.id,
                    payload={"kind": "synthetic", "owner": "second"},
                )
            )

        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=second.workspace.id,
            user_id=first.user.id,
        ) as session:
            leaked = session.execute(
                text("SELECT id FROM campaign_drafts WHERE id = 'p3-rls-second-draft'")
            ).all()
            assert leaked == []
            with pytest.raises(DBAPIError):
                session.execute(
                    text(
                        """
                        INSERT INTO campaign_drafts (id, workspace_id, payload)
                        VALUES ('p3-rls-cross-tenant-insert', :workspace_id, $${"kind": "cross"}$$::jsonb)
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {"workspace_id": second.workspace.id},
                )

        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=second.workspace.id,
            user_id=first.user.id,
        ) as session:
            updated = session.execute(
                text(
                    """
                    UPDATE campaign_drafts
                    SET payload = $${"kind": "cross-update"}$$::jsonb
                    WHERE id = 'p3-rls-second-draft' AND workspace_id = :workspace_id
                    """
                ),
                {"workspace_id": second.workspace.id},
            )
            assert updated.rowcount == 0
            with pytest.raises(DBAPIError):
                session.execute(
                    text(
                        """
                        DELETE FROM campaign_drafts
                        WHERE id = 'p3-rls-second-draft' AND workspace_id = :workspace_id
                        """
                    ),
                    {"workspace_id": second.workspace.id},
                )

        with app_runtime.sessions() as session:
            with session.begin():
                set_local_tenant_context(
                    session,
                    TenantContext(workspace_id=second.workspace.id, user_id=first.user.id),
                )
                with pytest.raises((DBAPIError, IntegrityError)):
                    session.execute(
                        text(
                            """
                            INSERT INTO memberships (
                                id, workspace_id, user_id, role, status, created_at, accepted_at
                            ) VALUES (
                                :id, :workspace_id, :user_id, 'owner', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {
                            "id": uuid4(),
                            "workspace_id": second.workspace.id,
                            "user_id": first.user.id,
                        },
                    )

        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=second.workspace.id,
            user_id=second.user.id,
        ) as session:
            assert (
                session.execute(
                    text("SELECT payload->>'kind' FROM campaign_drafts WHERE id = 'p3-rls-second-draft'")
                ).scalar_one()
                == "synthetic"
            )

        with app_runtime.engine.begin() as connection:
            connection.execute(text("SET LOCAL row_security = off"))
            with pytest.raises(DBAPIError):
                connection.execute(text("SELECT id FROM campaign_drafts"))

        attributes = get_role_attributes(owner_runtime)
        assert attributes.is_superuser is False
        assert attributes.can_bypass_rls is False
        assert get_table_owner(owner_runtime, "campaign_drafts") == "directpilot_owner"
    finally:
        owner_runtime.close()
        app_runtime.close()
