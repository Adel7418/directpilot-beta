from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.rls import WorkspaceContextError, tenant_transaction
from app.db.roles import bootstrap_database_roles
from app.models import CampaignDraftRequest, SemanticChangePackage, SemanticChangePreview
from app.modules.actions.idempotency import PostgresIdempotencyRepository
from app.modules.audit.repository import PostgresAuditRepository
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.tenancy.authorization import PostgresWorkspaceAuthorizer
from app.modules.tenancy.policy import AuthorizationDenied, Capability
from app.repositories.postgres_store import PostgresLegacyStoreRepository


_TENANT_TABLES = (
    "audit_events",
    "idempotency_records",
    "campaign_drafts",
    "semantic_change_packages",
)


@pytest.mark.integration
def test_two_users_are_denied_cross_tenant_service_repository_and_raw_sql_access(
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
            display_name="Synthetic isolation first user",
            workspace_name="Synthetic isolation first workspace",
            yandex_subject="synthetic-isolation-first-001",
        )
        second = identity.create_personal_workspace(
            display_name="Synthetic isolation second user",
            workspace_name="Synthetic isolation second workspace",
            yandex_subject="synthetic-isolation-second-001",
        )

        authorizer = PostgresWorkspaceAuthorizer(app_runtime.sessions)
        for capability in (Capability.READ_WORKSPACE_DATA, Capability.CREATE_PROPOSAL):
            with pytest.raises(AuthorizationDenied):
                authorizer.authorize(
                    user_id=first.user.id,
                    workspace_id=second.workspace.id,
                    capability=capability,
                )

        first_store = PostgresLegacyStoreRepository(
            app_runtime.sessions,
            workspace_id=first.workspace.id,
            user_id=first.user.id,
        )
        second_store = PostgresLegacyStoreRepository(
            app_runtime.sessions,
            workspace_id=second.workspace.id,
            user_id=second.user.id,
        )
        first_draft = first_store.create_draft(
            CampaignDraftRequest(
                business_type="isolation-first",
                region="Synthetic isolation first region",
                monthly_budget=101,
                landing_url="https://example.invalid/isolation-first",
            )
        )
        second_draft = second_store.create_draft(
            CampaignDraftRequest(
                business_type="isolation-second",
                region="Synthetic isolation second region",
                monthly_budget=202,
                landing_url="https://example.invalid/isolation-second",
            )
        )
        assert first_draft.id == second_draft.id
        assert first_store.get_draft(first_draft.id).business_type == "isolation-first"
        assert second_store.get_draft(second_draft.id).business_type == "isolation-second"

        first_package = SemanticChangePackage(
            package_id="synthetic-isolation-package",
            campaign_id="synthetic-first-campaign",
            mode="mock",
            created_at="2026-09-04T00:00:00Z",
            preview=SemanticChangePreview(operations=[]),
        )
        second_package = first_package.model_copy(
            update={"campaign_id": "synthetic-second-campaign"}
        )
        first_store.save_semantic_package(first_package)
        second_store.save_semantic_package(second_package)
        assert first_store.get_semantic_package(first_package.package_id) == first_package
        assert second_store.get_semantic_package(second_package.package_id) == second_package

        first_audit = PostgresAuditRepository(
            app_runtime.sessions,
            workspace_id=first.workspace.id,
            user_id=first.user.id,
        )
        second_audit = PostgresAuditRepository(
            app_runtime.sessions,
            workspace_id=second.workspace.id,
            user_id=second.user.id,
        )
        first_audit.append_audit("synthetic.isolation.first", "first")
        second_audit.append_audit("synthetic.isolation.second", "second")
        assert "synthetic.isolation.second" not in {
            event.action for event in first_audit.audit_events
        }
        assert "synthetic.isolation.first" not in {
            event.action for event in second_audit.audit_events
        }

        first_idempotency = PostgresIdempotencyRepository(
            app_runtime.sessions,
            workspace_id=first.workspace.id,
            user_id=first.user.id,
        )
        second_idempotency = PostgresIdempotencyRepository(
            app_runtime.sessions,
            workspace_id=second.workspace.id,
            user_id=second.user.id,
        )
        assert first_idempotency.claim("synthetic-isolation", "same-key", {"tenant": 1}).is_owner
        assert second_idempotency.claim("synthetic-isolation", "same-key", {"tenant": 2}).is_owner

        with app_runtime.sessions() as session:
            with session.begin():
                for table_name in _TENANT_TABLES:
                    assert session.execute(
                        text(f"SELECT workspace_id FROM {table_name}")
                    ).all() == []

        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=first.workspace.id,
            user_id=first.user.id,
        ) as session:
            for table_name in _TENANT_TABLES:
                assert session.execute(
                    text(
                        f"SELECT workspace_id FROM {table_name} "
                        "WHERE workspace_id = CAST(:workspace_id AS uuid)"
                    ),
                    {"workspace_id": str(second.workspace.id)},
                ).all() == []

            forbidden_inserts = (
                (
                    text(
                        "INSERT INTO audit_events "
                        "(id, workspace_id, actor, action, entity, dry_run) "
                        "VALUES (:id, CAST(:workspace_id AS uuid), "
                        "'synthetic', 'synthetic.raw', 'synthetic', true)"
                    ),
                    {"id": uuid4(), "workspace_id": str(second.workspace.id)},
                ),
                (
                    text(
                        "INSERT INTO idempotency_records "
                        "(id, workspace_id, namespace, key_hash, request_hash, status) "
                        "VALUES (:id, CAST(:workspace_id AS uuid), "
                        "'synthetic.raw', 'a'::text, 'b'::text, 'pending')"
                    ),
                    {"id": uuid4(), "workspace_id": str(second.workspace.id)},
                ),
                (
                    text(
                        "INSERT INTO campaign_drafts (id, workspace_id, payload) "
                        "VALUES ('synthetic-raw-draft', CAST(:workspace_id AS uuid), "
                        "CAST('{\"synthetic\": true}' AS jsonb))"
                    ),
                    {"workspace_id": str(second.workspace.id)},
                ),
                (
                    text(
                        "INSERT INTO semantic_change_packages (package_id, workspace_id, payload) "
                        "VALUES ('synthetic-raw-package', CAST(:workspace_id AS uuid), "
                        "CAST('{\"synthetic\": true}' AS jsonb))"
                    ),
                    {"workspace_id": str(second.workspace.id)},
                ),
            )
            for statement, parameters in forbidden_inserts:
                with pytest.raises(DBAPIError):
                    with session.begin_nested():
                        session.execute(statement, parameters)

        with pytest.raises(WorkspaceContextError):
            with tenant_transaction(app_runtime.sessions, workspace_id="not-a-workspace"):
                pass

        rollback_id = "synthetic-rollback-draft"
        with pytest.raises(RuntimeError, match="rollback"):
            with tenant_transaction(
                app_runtime.sessions,
                workspace_id=first.workspace.id,
                user_id=first.user.id,
            ) as session:
                session.execute(
                    text(
                        "INSERT INTO campaign_drafts (id, workspace_id, payload) "
                        "VALUES (:id, CAST(:workspace_id AS uuid), "
                        "CAST('{\"synthetic\": true}' AS jsonb))"
                    ),
                    {"id": rollback_id, "workspace_id": str(first.workspace.id)},
                )
                raise RuntimeError("rollback")
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=first.workspace.id,
            user_id=first.user.id,
        ) as session:
            assert session.execute(
                text("SELECT id FROM campaign_drafts WHERE id = :id"),
                {"id": rollback_id},
            ).all() == []
    finally:
        owner_runtime.close()
        app_runtime.close()
