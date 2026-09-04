from __future__ import annotations

from dataclasses import replace
from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient

from app.bootstrap.application import create_app
from app.bootstrap.dependencies import create_application_dependencies
from app.modules.sessions.cookies import SESSION_COOKIE_NAME


def _cookie_attributes(response: object) -> dict[str, str]:
    parsed = SimpleCookie()
    parsed.load(getattr(response, "headers")["set-cookie"])
    morsel = parsed[SESSION_COOKIE_NAME]
    return {attribute: morsel[attribute] for attribute in morsel.keys()}


@pytest.mark.integration
def test_cookie_auth_rejects_csrf_and_cross_origin_requests_and_sets_security_headers(
    postgres_service: object,
) -> None:
    from app.db.engine import DatabaseSettings, create_database_runtime
    from app.db.migrations.runner import upgrade_database
    from app.db.roles import bootstrap_database_roles
    from app.modules.identity.repository import PostgresIdentityRepository
    from app.modules.sessions.service import PostgresSessionService
    from app.modules.tenancy.authorization import PostgresWorkspaceAuthorizer

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
            identity_repository=PostgresIdentityRepository(app_runtime.sessions),
            session_service=PostgresSessionService(app_runtime.sessions),
            workspace_authorizer=PostgresWorkspaceAuthorizer(app_runtime.sessions),
            fake_auth_enabled=True,
        )
        client = TestClient(create_app(dependencies=dependencies), base_url="https://testserver")

        login = client.post(
            "/api/v1/_test/identity/login",
            json={"display_name": "Synthetic HTTP user"},
        )
        assert login.status_code == 200
        attributes = _cookie_attributes(login)
        assert attributes["path"] == "/"
        assert attributes["domain"] == ""
        assert attributes["secure"] is True
        assert attributes["httponly"] is True
        assert attributes["samesite"].lower() == "lax"
        assert login.headers["content-security-policy"].startswith("default-src 'self'")
        assert login.headers["x-content-type-options"] == "nosniff"
        assert login.headers["x-frame-options"] == "DENY"
        assert "strict-transport-security" in login.headers

        csrf_token = login.json()["csrf_token"]
        assert client.post("/api/v1/_test/identity/logout").status_code == 403
        assert (
            client.post(
                "/api/v1/_test/identity/logout",
                headers={
                    "X-CSRF-Token": csrf_token,
                    "Origin": "https://attacker.invalid",
                },
            ).status_code
            == 403
        )

        session_view = client.get("/api/v1/_test/identity/session")
        assert session_view.status_code == 200
        assert (
            client.post(
                "/api/v1/_test/identity/logout",
                headers={
                    "X-CSRF-Token": csrf_token,
                    "Origin": "https://testserver",
                },
            ).status_code
            == 204
        )
        assert client.get("/api/v1/_test/identity/session").status_code == 401
    finally:
        owner_runtime.close()
        app_runtime.close()
