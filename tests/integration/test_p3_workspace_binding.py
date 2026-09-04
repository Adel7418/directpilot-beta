from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.bootstrap.application import create_app
from app.bootstrap.dependencies import create_application_dependencies, legacy_store
from app.core.request_context import get_request_context
from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.models import CampaignDraftRequest
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.sessions.service import PostgresSessionService
from app.modules.tenancy.authorization import PostgresWorkspaceAuthorizer
from app.repositories.postgres_store import PostgresLegacyStoreRepository


@pytest.mark.integration
def test_cookie_session_binds_the_server_authorized_workspace_for_repository_access(
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
        dependencies = replace(
            create_application_dependencies(),
            repository=PostgresLegacyStoreRepository(app_runtime.sessions),
            identity_repository=PostgresIdentityRepository(app_runtime.sessions),
            session_service=PostgresSessionService(app_runtime.sessions),
            workspace_authorizer=PostgresWorkspaceAuthorizer(app_runtime.sessions),
            fake_auth_enabled=True,
        )
        app = create_app(dependencies=dependencies)

        @app.get("/api/v1/_test/workspace-binding")
        def workspace_binding() -> dict[str, object]:
            context = get_request_context()
            if context.workspace_id is None:
                raise HTTPException(status_code=401, detail="authentication required")
            return {
                "workspace_id": str(context.workspace_id),
                "business_types": sorted(
                    draft.business_type for draft in legacy_store.drafts.values()
                ),
            }

        @app.post("/api/v1/_test/workspace-binding/draft")
        def create_workspace_bound_draft() -> dict[str, str]:
            context = get_request_context()
            if context.workspace_id is None:
                raise HTTPException(status_code=401, detail="authentication required")
            draft = legacy_store.create_draft(
                CampaignDraftRequest(
                    business_type="first-http-write",
                    region="Synthetic HTTP region",
                    monthly_budget=303,
                    landing_url="https://example.invalid/http-write",
                )
            )
            return {"draft_id": draft.id, "workspace_id": str(context.workspace_id)}

        first_client = TestClient(app, base_url="https://testserver")
        second_client = TestClient(app, base_url="https://testserver")
        first_login = first_client.post(
            "/api/v1/_test/identity/login",
            json={"display_name": "Synthetic HTTP first user"},
        )
        second_login = second_client.post(
            "/api/v1/_test/identity/login",
            json={"display_name": "Synthetic HTTP second user"},
        )
        assert first_login.status_code == 200
        assert second_login.status_code == 200
        first_workspace_id = first_login.json()["workspace_id"]
        second_workspace_id = second_login.json()["workspace_id"]

        first_repository = PostgresLegacyStoreRepository(
            app_runtime.sessions,
            workspace_id=first_workspace_id,
        )
        second_repository = PostgresLegacyStoreRepository(
            app_runtime.sessions,
            workspace_id=second_workspace_id,
        )
        first_repository.create_draft(
            CampaignDraftRequest(
                business_type="first-http-read",
                region="Synthetic HTTP first region",
                monthly_budget=101,
                landing_url="https://example.invalid/http-first",
            )
        )
        second_repository.create_draft(
            CampaignDraftRequest(
                business_type="second-http-read",
                region="Synthetic HTTP second region",
                monthly_budget=202,
                landing_url="https://example.invalid/http-second",
            )
        )

        forged_workspace_header = {"X-Workspace-ID": second_workspace_id}
        first_read = first_client.get(
            "/api/v1/_test/workspace-binding",
            headers=forged_workspace_header,
        )
        assert first_read.status_code == 200
        assert first_read.json() == {
            "workspace_id": first_workspace_id,
            "business_types": ["first-http-read"],
        }
        assert second_client.get("/api/v1/_test/workspace-binding").json() == {
            "workspace_id": second_workspace_id,
            "business_types": ["second-http-read"],
        }

        first_write = first_client.post(
            "/api/v1/_test/workspace-binding/draft",
            headers={
                "Origin": "https://testserver",
                "X-CSRF-Token": first_login.json()["csrf_token"],
                "X-Workspace-ID": second_workspace_id,
            },
        )
        assert first_write.status_code == 200
        assert first_write.json()["workspace_id"] == first_workspace_id
        assert second_client.get("/api/v1/_test/workspace-binding").json() == {
            "workspace_id": second_workspace_id,
            "business_types": ["second-http-read"],
        }
        assert first_client.get("/api/v1/_test/workspace-binding").json()[
            "business_types"
        ] == ["first-http-read", "first-http-write"]
        assert TestClient(app, base_url="https://testserver").get(
            "/api/v1/_test/workspace-binding"
        ).status_code == 401
    finally:
        owner_runtime.close()
        app_runtime.close()
