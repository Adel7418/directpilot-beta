from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.bootstrap.application import create_app
from app.bootstrap.dependencies import create_application_dependencies
from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.sessions.service import PostgresSessionService
from app.modules.tenancy.authorization import PostgresWorkspaceAuthorizer
from app.modules.tenancy.models import MembershipRole, MembershipStatus


@pytest.mark.integration
def test_session_route_authorizes_the_current_membership_not_cookie_authority(
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
        dependencies = replace(
            create_application_dependencies(),
            identity_repository=identity,
            session_service=PostgresSessionService(app_runtime.sessions),
            workspace_authorizer=PostgresWorkspaceAuthorizer(app_runtime.sessions),
            fake_auth_enabled=True,
        )
        client = TestClient(create_app(dependencies=dependencies), base_url="https://testserver")

        login = client.post(
            "/api/v1/_test/identity/login",
            json={"display_name": "Synthetic dynamic authorization user"},
        )
        payload = login.json()
        assert login.status_code == 200

        identity.update_membership(
            workspace_id=payload["workspace_id"],
            user_id=payload["user_id"],
            role=MembershipRole.VIEWER,
            status=MembershipStatus.REVOKED,
        )

        assert client.get("/api/v1/_test/identity/session").status_code == 403
    finally:
        owner_runtime.close()
        app_runtime.close()
