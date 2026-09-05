from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bootstrap.application import create_app
from app.bootstrap.dependencies import create_application_dependencies
from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.models import ExternalIdentityRecord
from app.db.rls import tenant_transaction
from app.db.roles import bootstrap_database_roles
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.integrations.yandex.oauth import (
    YandexOAuthConfiguration,
    YandexOAuthIntegration,
    YandexOAuthTokenSet,
    YandexUserInfo,
)
from app.modules.integrations.yandex.provider import YandexOAuthProviderFailure
from app.modules.integrations.yandex.repository import (
    PostgresExternalIdentityRepository,
    PostgresOAuthTransactionRepository,
)
from app.modules.sessions.cookies import SESSION_COOKIE_NAME
from app.modules.sessions.service import PostgresSessionService
from app.modules.tenancy.authorization import PostgresWorkspaceAuthorizer
from app.repositories.postgres_store import PostgresLegacyStoreRepository


class _MockYandexOAuthProvider:
    def __init__(self, config: YandexOAuthConfiguration) -> None:
        self._config = config
        self.exchange_calls = 0
        self.userinfo_calls = 0

    def exchange_code(self, *, code: str, code_verifier: str) -> YandexOAuthTokenSet:
        assert len(code) >= 43
        assert len(code_verifier) >= 43
        self.exchange_calls += 1
        return YandexOAuthTokenSet(
            access_token=secrets.token_urlsafe(32),
            refresh_token=secrets.token_urlsafe(32),
            expires_in=3600,
        )

    def fetch_user_info(self, *, access_token: str) -> YandexUserInfo:
        assert len(access_token) >= 43
        self.userinfo_calls += 1
        return YandexUserInfo(
            subject="stable-yandex-user-id",
            client_id=self._config.client_id,
            login="synthetic-login",
            display_name="Synthetic display name",
        )


@pytest.mark.integration
def test_authenticated_oauth_callback_rotates_session_and_maps_stable_subject(
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
        config = YandexOAuthConfiguration(
            client_id="synthetic-client-id",
            client_secret="synthetic-client-secret",
            redirect_uri="http://127.0.0.1:8000/api/v1/integrations/yandex/callback",
        )
        provider = _MockYandexOAuthProvider(config)
        session_service = PostgresSessionService(app_runtime.sessions)
        dependencies = replace(
            create_application_dependencies(),
            repository=PostgresLegacyStoreRepository(app_runtime.sessions),
            identity_repository=PostgresIdentityRepository(app_runtime.sessions),
            session_service=session_service,
            workspace_authorizer=PostgresWorkspaceAuthorizer(app_runtime.sessions),
            fake_auth_enabled=True,
            yandex_oauth=YandexOAuthIntegration(
                config=config,
                transactions=PostgresOAuthTransactionRepository(app_runtime.sessions),
                identities=PostgresExternalIdentityRepository(app_runtime.sessions),
                provider=provider,
            ),
        )
        client = TestClient(create_app(dependencies=dependencies), base_url="https://testserver")

        login = client.post(
            "/api/v1/_test/identity/login",
            json={"display_name": "Synthetic OAuth HTTP owner"},
        )
        assert login.status_code == 200
        previous_session_token = client.cookies.get(SESSION_COOKIE_NAME)
        assert previous_session_token is not None
        user_id = login.json()["user_id"]
        workspace_id = login.json()["workspace_id"]

        start = client.get(
            "/api/v1/integrations/yandex/start?return_path=/",
            follow_redirects=False,
        )
        assert start.status_code == 302
        state = parse_qs(urlsplit(start.headers["location"]).query, strict_parsing=True)[
            "state"
        ][0]
        callback = client.get(
            "/api/v1/integrations/yandex/callback",
            params={"code": secrets.token_urlsafe(32), "state": state},
            follow_redirects=False,
        )

        assert callback.status_code == 303
        assert callback.headers["location"] == "/"
        rotated_session_token = client.cookies.get(SESSION_COOKIE_NAME)
        assert rotated_session_token is not None
        assert not hmac.compare_digest(previous_session_token, rotated_session_token)
        assert session_service.authenticate(previous_session_token) is None
        assert session_service.authenticate(rotated_session_token) is not None
        assert provider.exchange_calls == 1
        assert provider.userinfo_calls == 1
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=workspace_id,
            user_id=user_id,
        ) as session:
            identity = session.scalar(select(ExternalIdentityRecord))
        assert identity is not None
        assert identity.subject == "stable-yandex-user-id"
        assert identity.user_id == UUID(login.json()["user_id"])
    finally:
        owner_runtime.close()
        app_runtime.close()


class _FailingYandexOAuthProvider:
    def __init__(self) -> None:
        self.exchange_calls = 0
        self.userinfo_calls = 0

    def exchange_code(self, *, code: str, code_verifier: str) -> YandexOAuthTokenSet:
        assert len(code) >= 43
        assert len(code_verifier) >= 43
        self.exchange_calls += 1
        raise YandexOAuthProviderFailure("provider_error")

    def fetch_user_info(self, *, access_token: str) -> YandexUserInfo:
        self.userinfo_calls += 1
        raise AssertionError("userinfo must not be called after a token-exchange failure")


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    return any(marker in value for marker in markers)


@pytest.mark.integration
def test_oauth_route_errors_do_not_echo_callback_or_secret_values(
    postgres_service: object,
    caplog: pytest.LogCaptureFixture,
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
        config = YandexOAuthConfiguration(
            client_id="synthetic-client-id",
            client_secret="synthetic-client-secret",
            redirect_uri="http://127.0.0.1:8000/api/v1/integrations/yandex/callback",
        )
        provider = _FailingYandexOAuthProvider()
        dependencies = replace(
            create_application_dependencies(),
            repository=PostgresLegacyStoreRepository(app_runtime.sessions),
            identity_repository=PostgresIdentityRepository(app_runtime.sessions),
            session_service=PostgresSessionService(app_runtime.sessions),
            workspace_authorizer=PostgresWorkspaceAuthorizer(app_runtime.sessions),
            fake_auth_enabled=True,
            yandex_oauth=YandexOAuthIntegration(
                config=config,
                transactions=PostgresOAuthTransactionRepository(app_runtime.sessions),
                identities=PostgresExternalIdentityRepository(app_runtime.sessions),
                provider=provider,
            ),
        )
        client = TestClient(create_app(dependencies=dependencies), base_url="https://testserver")
        login = client.post(
            "/api/v1/_test/identity/login",
            json={"display_name": "Synthetic OAuth error owner"},
        )
        assert login.status_code == 200
        start = client.get("/api/v1/integrations/yandex/start", follow_redirects=False)
        assert start.status_code == 302
        state = parse_qs(urlsplit(start.headers["location"]).query, strict_parsing=True)[
            "state"
        ][0]
        code = secrets.token_urlsafe(32)
        caplog.set_level(logging.INFO, logger="directpilot.request")
        provider_failure = client.get(
            "/api/v1/integrations/yandex/callback",
            params={"code": code, "state": state},
            follow_redirects=False,
        )
        malformed = client.get(
            "/api/v1/integrations/yandex/callback",
            params={"state": secrets.token_urlsafe(32)},
            follow_redirects=False,
        )
        open_redirect = client.get(
            "/api/v1/integrations/yandex/start",
            params={"return_path": "https://attacker.invalid/"},
            follow_redirects=False,
        )

        assert provider_failure.status_code == 502
        assert malformed.status_code == 400
        assert open_redirect.status_code == 400
        assert provider.exchange_calls == 1
        assert provider.userinfo_calls == 0
        sensitive_markers = (code, state, config.client_secret)
        assert not _contains_any(provider_failure.text, sensitive_markers)
        assert not _contains_any(malformed.text, sensitive_markers)
        assert not _contains_any(open_redirect.text, sensitive_markers)
        log_output = "\n".join(str(record.__dict__) for record in caplog.records)
        assert not _contains_any(log_output, sensitive_markers)
    finally:
        owner_runtime.close()
        app_runtime.close()
