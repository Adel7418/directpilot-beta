from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bootstrap.application import create_app
from app.bootstrap.dependencies import create_application_dependencies
from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.models import YandexProviderConnectionRecord
from app.db.rls import tenant_transaction
from app.db.roles import bootstrap_database_roles
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.integrations.yandex.credentials import (
    CredentialContext,
    CredentialKeyRing,
    CredentialVault,
    EncryptedYandexCredential,
    YandexCredentialPayload,
)
from app.modules.integrations.yandex.oauth import (
    YandexOAuthConfiguration,
    YandexOAuthIntegration,
    YandexOAuthTokenSet,
    YandexUserInfo,
)
from app.modules.integrations.yandex.repository import (
    PostgresExternalIdentityRepository,
    PostgresOAuthTransactionRepository,
    PostgresYandexProviderConnectionRepository,
)
from app.modules.sessions.service import PostgresSessionService
from app.modules.tenancy.authorization import PostgresWorkspaceAuthorizer
from app.repositories.postgres_store import PostgresLegacyStoreRepository


@pytest.mark.integration
def test_connection_repository_persists_only_an_authenticated_envelope(
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
    now = datetime(2026, 9, 6, 13, tzinfo=timezone.utc)
    access_token = "synthetic-access-token"
    refresh_token = "synthetic-refresh-token"
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        identity = PostgresIdentityRepository(app_runtime.sessions).create_personal_workspace(
            display_name="Synthetic credential owner",
            workspace_name="Synthetic credential workspace",
        )
        subject = "synthetic-stable-yandex-subject"
        PostgresExternalIdentityRepository(app_runtime.sessions).bind_yandex_identity(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            issuer="https://login.yandex.ru",
            subject=subject,
            profile_login="synthetic-login",
            profile_display_name="Synthetic display name",
            now=now,
        )
        vault = CredentialVault(
            CredentialKeyRing.from_keys(
                active_key_id="test-kek-v1",
                keys={"test-kek-v1": b"k" * 32},
            )
        )
        connections = PostgresYandexProviderConnectionRepository(
            app_runtime.sessions,
            vault=vault,
        )
        payload = YandexCredentialPayload(
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_expires_at=now,
            refresh_token_expires_at=None,
        )

        connection = connections.persist_yandex_oauth_tokens(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            issuer="https://login.yandex.ru",
            subject=subject,
            payload=payload,
            now=now,
        )

        assert connection.workspace_id == identity.workspace.id
        assert connection.status == "active"
        assert connection.access_token_expires_at == now
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=identity.workspace.id,
            user_id=identity.user.id,
        ) as session:
            record = session.scalar(select(YandexProviderConnectionRecord))
        assert record is not None
        assert record.id == connection.id
        assert record.token_ciphertext != access_token.encode()
        assert record.token_nonce != b""
        assert record.wrapped_dek != b""
        assert {"access_token", "refresh_token"}.isdisjoint(record.__table__.columns.keys())
        persisted = {column.name: getattr(record, column.name) for column in record.__table__.columns}
        assert access_token not in repr(persisted)
        assert refresh_token not in repr(persisted)
        decrypted = vault.decrypt(
            context=CredentialContext.for_yandex_connection(
                workspace_id=identity.workspace.id,
                connection_id=record.id,
            ),
            encrypted=EncryptedYandexCredential(
                token_ciphertext=record.token_ciphertext,
                token_nonce=record.token_nonce,
                wrapped_dek=record.wrapped_dek,
                wrap_nonce=record.wrap_nonce,
                kek_key_id=record.kek_key_id,
                schema_version=record.schema_version,
                access_token_expires_at=record.access_token_expires_at,
                refresh_token_expires_at=record.refresh_token_expires_at,
            ),
        )
        assert decrypted == payload
    finally:
        owner_runtime.close()
        app_runtime.close()


class _CallbackProvider:
    def __init__(self, config: YandexOAuthConfiguration) -> None:
        self._config = config

    def exchange_code(self, *, code: str, code_verifier: str) -> YandexOAuthTokenSet:
        assert code == "synthetic-authorization-code"
        assert code_verifier
        return YandexOAuthTokenSet(
            access_token="synthetic-access-token",
            refresh_token="synthetic-refresh-token",
            expires_in=3600,
        )

    def fetch_user_info(self, *, access_token: str) -> YandexUserInfo:
        assert access_token == "synthetic-access-token"
        return YandexUserInfo(
            subject="synthetic-stable-yandex-subject",
            client_id=self._config.client_id,
            login="synthetic-login",
            display_name="Synthetic display name",
        )


@pytest.mark.integration
def test_callback_persists_encrypted_credentials_and_redirects_without_secret_output(
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
    access_token = "synthetic-access-token"
    refresh_token = "synthetic-refresh-token"
    authorization_code = "synthetic-authorization-code"
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
        vault = CredentialVault(
            CredentialKeyRing.from_keys(
                active_key_id="test-kek-v1",
                keys={"test-kek-v1": b"k" * 32},
            )
        )
        connections = PostgresYandexProviderConnectionRepository(
            app_runtime.sessions,
            vault=vault,
        )
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
                provider=_CallbackProvider(config),
                credential_persister=connections,
            ),
        )
        client = TestClient(create_app(dependencies=dependencies), base_url="https://testserver")
        login = client.post(
            "/api/v1/_test/identity/login",
            json={"display_name": "Synthetic callback credential owner"},
        )
        assert login.status_code == 200
        user_id = UUID(login.json()["user_id"])
        workspace_id = UUID(login.json()["workspace_id"])
        started = client.get(
            "/api/v1/integrations/yandex/start",
            follow_redirects=False,
        )
        assert started.status_code == 302
        state = parse_qs(urlsplit(started.headers["location"]).query, strict_parsing=True)["state"][0]
        caplog.set_level(logging.INFO, logger="directpilot.request")

        callback = client.get(
            "/api/v1/integrations/yandex/callback",
            params={"code": authorization_code, "state": state},
            follow_redirects=False,
        )

        assert callback.status_code == 303
        assert callback.headers["location"] == "/"
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=workspace_id,
            user_id=user_id,
        ) as session:
            record = session.scalar(select(YandexProviderConnectionRecord))
        assert record is not None
        assert record.token_ciphertext != access_token.encode()
        assert record.token_ciphertext != refresh_token.encode()
        assert record.token_nonce
        assert record.wrapped_dek
        assert record.wrap_nonce
        assert {"access_token", "refresh_token"}.isdisjoint(record.__table__.columns.keys())
        persisted = {column.name: getattr(record, column.name) for column in record.__table__.columns}
        for forbidden_value in (access_token, refresh_token, authorization_code):
            assert forbidden_value not in callback.text
            assert forbidden_value not in repr(persisted)
            assert forbidden_value not in "\n".join(str(item.__dict__) for item in caplog.records)
        openapi = json.dumps(client.get("/openapi.json").json(), sort_keys=True)
        for forbidden_field in (
            "token_ciphertext",
            "token_nonce",
            "wrapped_dek",
            "wrap_nonce",
            "code_verifier",
        ):
            assert forbidden_field not in openapi
    finally:
        owner_runtime.close()
        app_runtime.close()
