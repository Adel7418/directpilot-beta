from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.bootstrap.application import create_app
from app.bootstrap.dependencies import create_application_dependencies
from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.integrations.yandex.oauth import YandexOAuthIntegration
from app.modules.integrations.yandex.refresh import DisconnectResult, RefreshResult
from app.modules.integrations.yandex.repository import (
    PostgresExternalIdentityRepository,
    PostgresOAuthTransactionRepository,
)
from app.modules.sessions.service import PostgresSessionService
from app.modules.tenancy.authorization import PostgresWorkspaceAuthorizer
from app.repositories.postgres_store import PostgresLegacyStoreRepository


class _RecordingLifecycle:
    def __init__(self) -> None:
        self.refresh_calls: list[tuple[UUID, UUID, UUID]] = []
        self.disconnect_calls: list[tuple[UUID, UUID, UUID]] = []

    def refresh(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        now: datetime | None = None,
    ) -> RefreshResult:
        del now
        self.refresh_calls.append((user_id, workspace_id, connection_id))
        return RefreshResult(
            connection_id=connection_id,
            status="active",
            refreshed=True,
            access_token_expires_at=datetime(2026, 9, 6, 13, tzinfo=timezone.utc),
            credential_version=2,
        )

    def disconnect(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        now: datetime | None = None,
    ) -> DisconnectResult:
        del now
        self.disconnect_calls.append((user_id, workspace_id, connection_id))
        return DisconnectResult(connection_id=connection_id)


@pytest.mark.integration
def test_refresh_and_disconnect_routes_require_csrf_and_return_safe_schemas(
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
        lifecycle = _RecordingLifecycle()
        dependencies = replace(
            create_application_dependencies(),
            repository=PostgresLegacyStoreRepository(app_runtime.sessions),
            identity_repository=PostgresIdentityRepository(app_runtime.sessions),
            session_service=PostgresSessionService(app_runtime.sessions),
            workspace_authorizer=PostgresWorkspaceAuthorizer(app_runtime.sessions),
            fake_auth_enabled=True,
            yandex_oauth=YandexOAuthIntegration(
                transactions=PostgresOAuthTransactionRepository(app_runtime.sessions),
                identities=PostgresExternalIdentityRepository(app_runtime.sessions),
            ),
            yandex_connection_lifecycle=lifecycle,
        )
        client = TestClient(create_app(dependencies=dependencies), base_url="https://testserver")
        connection_id = uuid4()
        refresh_path = f"/api/v1/integrations/yandex/connections/{connection_id}/refresh"
        disconnect_path = f"/api/v1/integrations/yandex/connections/{connection_id}/disconnect"

        assert client.post(refresh_path).status_code == 401
        login = client.post(
            "/api/v1/_test/identity/login",
            json={"display_name": "Synthetic lifecycle HTTP owner"},
        )
        assert login.status_code == 200
        user_id = UUID(login.json()["user_id"])
        workspace_id = UUID(login.json()["workspace_id"])
        csrf_token = login.json()["csrf_token"]
        assert client.post(refresh_path).status_code == 403

        headers = {"X-CSRF-Token": csrf_token, "Origin": "https://testserver"}
        refreshed = client.post(refresh_path, headers=headers)
        disconnected = client.post(disconnect_path, headers=headers)

        assert refreshed.status_code == 200
        assert refreshed.json() == {
            "connection_id": str(connection_id),
            "status": "active",
            "refreshed": True,
            "access_token_expires_at": "2026-09-06T13:00:00Z",
            "credential_version": 2,
        }
        assert disconnected.status_code == 200
        assert disconnected.json() == {
            "connection_id": str(connection_id),
            "status": "disconnected",
            "local_credentials_purged": True,
            "provider_revocation": "not_supported_for_current_grant",
            "yandex_revocation_url": "https://id.yandex.ru/personal/data-access",
        }
        assert lifecycle.refresh_calls == [(user_id, workspace_id, connection_id)]
        assert lifecycle.disconnect_calls == [(user_id, workspace_id, connection_id)]
        openapi = json.dumps(client.get("/openapi.json").json(), sort_keys=True)
        assert "/api/v1/integrations/yandex/connections/{connection_id}/refresh" in openapi
        assert "/api/v1/integrations/yandex/connections/{connection_id}/disconnect" in openapi
        for forbidden_field in (
            '"access_token"',
            '"refresh_token"',
            '"client_secret"',
            '"token_ciphertext"',
            '"wrapped_dek"',
        ):
            assert forbidden_field not in openapi
    finally:
        owner_runtime.close()
        app_runtime.close()
