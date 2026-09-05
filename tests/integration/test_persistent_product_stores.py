from __future__ import annotations

from collections.abc import Callable

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.models import (
    CampaignDraftRequest,
    LiveCreateCampaignRequest,
    SemanticChangePackage,
    SemanticChangePreview,
)
from app.repositories.postgres_store import PostgresLegacyStoreRepository


def test_product_draft_and_semantic_package_survive_process_and_database_restart(
    postgres_service: object,
    restart_postgres: Callable[[], None],
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
        repository = PostgresLegacyStoreRepository(app_runtime.sessions)
        draft = repository.create_draft(
            CampaignDraftRequest(
                business_type="services",
                region="Казань",
                monthly_budget=1000,
                landing_url="https://example.invalid/landing",
            )
        )
        repository.append_keywords(draft.id, ["services Kazan"])
        package = SemanticChangePackage(
            package_id="package-001",
            campaign_id="campaign-001",
            mode="mock",
            created_at="2026-09-04T00:00:00Z",
            preview=SemanticChangePreview(operations=[]),
        )
        repository.save_semantic_package(package)

        restart_postgres()
        app_runtime.close()
        reloaded_runtime = create_database_runtime(
            DatabaseSettings.from_mapping(
                {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "app_url")}
            )
        )
        try:
            reloaded = PostgresLegacyStoreRepository(reloaded_runtime.sessions)
            assert "services Kazan" in reloaded.drafts[draft.id].keywords
            assert reloaded.get_semantic_package(package.package_id) == package
            operation_repository = PostgresLegacyStoreRepository(reloaded_runtime.sessions)
            preview = operation_repository.live_create_campaign(
                LiveCreateCampaignRequest(
                    draft_id=draft.id,
                    approved=True,
                    idempotency_key="persistent-draft-preview-001",
                    dry_run=True,
                ),
                settings=None,
                client=None,
            )
            assert preview.draft_id == draft.id
            assert preview.applied is False
        finally:
            reloaded_runtime.close()
    finally:
        owner_runtime.close()
        app_runtime.close()
