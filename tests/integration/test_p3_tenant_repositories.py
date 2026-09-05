from __future__ import annotations

import pytest

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.models import CampaignDraftRequest, SemanticChangePackage, SemanticChangePreview
from app.modules.actions.idempotency import PostgresIdempotencyRepository
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.audit.repository import PostgresAuditRepository
from app.repositories.postgres_store import PostgresLegacyStoreRepository


@pytest.mark.integration
def test_tenant_repositories_scope_p2_rows_and_idempotency_by_workspace(
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
            display_name="Synthetic first repository user",
            workspace_name="Synthetic first repository workspace",
            yandex_subject="synthetic-repository-first-001",
        )
        second = identity.create_personal_workspace(
            display_name="Synthetic second repository user",
            workspace_name="Synthetic second repository workspace",
            yandex_subject="synthetic-repository-second-001",
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
                business_type="first",
                region="Synthetic first region",
                monthly_budget=101,
                landing_url="https://example.invalid/first",
            )
        )
        second_draft = second_store.create_draft(
            CampaignDraftRequest(
                business_type="second",
                region="Synthetic second region",
                monthly_budget=202,
                landing_url="https://example.invalid/second",
            )
        )
        assert first_draft.id == second_draft.id
        assert first_store.drafts[first_draft.id].business_type == "first"
        assert second_store.drafts[second_draft.id].business_type == "second"

        first_package = SemanticChangePackage(
            package_id="same-synthetic-package",
            campaign_id="first-campaign",
            mode="mock",
            created_at="2026-09-04T00:00:00Z",
            preview=SemanticChangePreview(operations=[]),
        )
        second_package = first_package.model_copy(update={"campaign_id": "second-campaign"})
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
        first_audit.append_audit("synthetic.first", "first-row")
        second_audit.append_audit("synthetic.second", "second-row")
        first_actions = [event.action for event in first_audit.audit_events]
        second_actions = [event.action for event in second_audit.audit_events]
        assert "synthetic.first" in first_actions
        assert "synthetic.second" not in first_actions
        assert "synthetic.second" in second_actions
        assert "synthetic.first" not in second_actions

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
        assert first_idempotency.claim("synthetic.operation", "same-key", {"value": 1}).is_owner
        assert second_idempotency.claim("synthetic.operation", "same-key", {"value": 1}).is_owner
    finally:
        owner_runtime.close()
        app_runtime.close()
