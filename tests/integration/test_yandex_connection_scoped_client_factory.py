from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.models import ProviderAccountRecord, YandexProviderConnectionRecord
from app.db.rls import tenant_transaction
from app.db.roles import bootstrap_database_roles
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.integrations.yandex.client_factory import (
    DirectClientBindingNotFound,
    DirectClientClosed,
    DirectClientCredentialUnavailable,
    DirectClientLeaseInvalidated,
    DirectClientRequest,
    PostgresConnectionScopedDirectClientFactory,
)
from app.modules.integrations.yandex.credentials import (
    CredentialKeyRing,
    CredentialVault,
    ProviderAccountLoginContext,
    YandexCredentialPayload,
)
from app.modules.integrations.yandex.repository import (
    PostgresExternalIdentityRepository,
    PostgresYandexProviderConnectionRepository,
)
from app.modules.tenancy.authorization import PostgresWorkspaceAuthorizer


@dataclass(frozen=True, slots=True)
class _Binding:
    user_id: UUID
    workspace_id: UUID
    connection_id: UUID
    provider_account_id: UUID


def _vault() -> CredentialVault:
    return CredentialVault(
        CredentialKeyRing.from_keys(
            active_key_id="test-kek-v1",
            keys={"test-kek-v1": b"k" * 32},
        )
    )


def _create_binding(*, sessions, vault: CredentialVault, now: datetime) -> _Binding:
    identity = PostgresIdentityRepository(sessions).create_personal_workspace(
        display_name="Synthetic connection owner",
        workspace_name="Synthetic connection workspace",
    )
    subject = f"synthetic-subject-{identity.user.id}"
    PostgresExternalIdentityRepository(sessions).bind_yandex_identity(
        user_id=identity.user.id,
        workspace_id=identity.workspace.id,
        issuer="https://login.yandex.ru",
        subject=subject,
        profile_login="synthetic-profile-login",
        profile_display_name="Synthetic profile",
        now=now,
    )
    connection = PostgresYandexProviderConnectionRepository(sessions, vault=vault).persist_yandex_oauth_tokens(
        user_id=identity.user.id,
        workspace_id=identity.workspace.id,
        issuer="https://login.yandex.ru",
        subject=subject,
        payload=YandexCredentialPayload(
            access_token="connection-credential-for-test",
            refresh_token="refresh-credential-for-test",
            access_token_expires_at=now + timedelta(hours=1),
            refresh_token_expires_at=None,
        ),
        now=now,
    )
    provider_account_id = uuid4()
    encrypted_login = vault.encrypt_provider_account_login(
        context=ProviderAccountLoginContext.for_yandex_provider_account(
            workspace_id=identity.workspace.id,
            connection_id=connection.id,
            provider_account_id=provider_account_id,
        ),
        login="synthetic-routing-login",
    )
    with tenant_transaction(
        sessions,
        workspace_id=identity.workspace.id,
        user_id=identity.user.id,
    ) as session:
        session.add(
            ProviderAccountRecord(
                id=provider_account_id,
                workspace_id=identity.workspace.id,
                connection_id=connection.id,
                provider_account_key="synthetic-account-key",
                account_type="advertiser",
                display_name="Synthetic advertiser",
                status="active",
                capabilities=["direct.read"],
                country_id=None,
                currency=None,
                login_ciphertext=encrypted_login.ciphertext,
                login_nonce=encrypted_login.nonce,
                login_wrapped_dek=encrypted_login.wrapped_dek,
                login_wrap_nonce=encrypted_login.wrap_nonce,
                login_kek_key_id=encrypted_login.kek_key_id,
                login_schema_version=encrypted_login.schema_version,
                last_discovered_at=now,
                last_verified_at=now,
                created_at=now,
                updated_at=now,
                version=1,
            )
        )
        session.flush()
    return _Binding(
        user_id=identity.user.id,
        workspace_id=identity.workspace.id,
        connection_id=connection.id,
        provider_account_id=provider_account_id,
    )


@pytest.mark.integration
def test_postgresql_factory_denies_cross_workspace_and_invalidated_lease_before_transport(
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
    now = datetime.now(timezone.utc)
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        vault = _vault()
        binding = _create_binding(sessions=app_runtime.sessions, vault=vault, now=now)
        other_identity = PostgresIdentityRepository(app_runtime.sessions).create_personal_workspace(
            display_name="Synthetic other owner",
            workspace_name="Synthetic other workspace",
        )

        transport_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            nonlocal transport_calls
            transport_calls += 1
            return httpx.Response(200, json={"result": {}})

        factory = PostgresConnectionScopedDirectClientFactory(
            sessions=app_runtime.sessions,
            vault=vault,
            workspace_authorizer=PostgresWorkspaceAuthorizer(app_runtime.sessions),
            settings=Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token=None),
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(DirectClientBindingNotFound):
            factory.create(
                request=DirectClientRequest(
                    user_id=other_identity.user.id,
                    workspace_id=other_identity.workspace.id,
                    provider_connection_id=binding.connection_id,
                    provider_account_id=binding.provider_account_id,
                    request_id="cross-workspace-request",
                    runtime_mode="sandbox",
                )
            )
        assert transport_calls == 0

        lease = factory.create(
            request=DirectClientRequest(
                user_id=binding.user_id,
                workspace_id=binding.workspace_id,
                provider_connection_id=binding.connection_id,
                provider_account_id=binding.provider_account_id,
                request_id="lease-version-request",
                runtime_mode="live_write",
            )
        )
        lease.client.clients_get()
        calls_before_invalidation = transport_calls
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=binding.workspace_id,
            user_id=binding.user_id,
        ) as session:
            connection = session.scalar(
                select(YandexProviderConnectionRecord).where(
                    YandexProviderConnectionRecord.id == binding.connection_id
                )
            )
            assert connection is not None
            connection.version += 1
            session.flush()

        with pytest.raises(DirectClientLeaseInvalidated):
            lease.client.clients_get()
        assert transport_calls == calls_before_invalidation

        lease.close()
        lease.close()
        with pytest.raises(DirectClientClosed):
            _ = lease.client
    finally:
        owner_runtime.close()
        app_runtime.close()


@pytest.mark.integration
def test_postgresql_factory_rejects_tampered_credential_or_routing_login_without_transport(
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
    now = datetime.now(timezone.utc)
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        vault = _vault()
        token_binding = _create_binding(sessions=app_runtime.sessions, vault=vault, now=now)
        login_binding = _create_binding(sessions=app_runtime.sessions, vault=vault, now=now)
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=token_binding.workspace_id,
            user_id=token_binding.user_id,
        ) as session:
            connection = session.scalar(
                select(YandexProviderConnectionRecord).where(
                    YandexProviderConnectionRecord.id == token_binding.connection_id
                )
            )
            assert connection is not None
            connection.token_ciphertext = b"tampered"
            session.flush()
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=login_binding.workspace_id,
            user_id=login_binding.user_id,
        ) as session:
            account = session.scalar(
                select(ProviderAccountRecord).where(
                    ProviderAccountRecord.id == login_binding.provider_account_id
                )
            )
            assert account is not None
            account.account_type = "agency_client"
            account.login_ciphertext = b"tampered"
            session.flush()

        transport_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            nonlocal transport_calls
            transport_calls += 1
            return httpx.Response(200, json={"result": {}})

        factory = PostgresConnectionScopedDirectClientFactory(
            sessions=app_runtime.sessions,
            vault=vault,
            workspace_authorizer=PostgresWorkspaceAuthorizer(app_runtime.sessions),
            settings=Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token=None),
            transport=httpx.MockTransport(handler),
        )
        for binding, request_id in (
            (token_binding, "tampered-token-request"),
            (login_binding, "tampered-login-request"),
        ):
            with pytest.raises(DirectClientCredentialUnavailable):
                factory.create(
                    request=DirectClientRequest(
                        user_id=binding.user_id,
                        workspace_id=binding.workspace_id,
                        provider_connection_id=binding.connection_id,
                        provider_account_id=binding.provider_account_id,
                        request_id=request_id,
                        runtime_mode="sandbox",
                    )
                )
        assert transport_calls == 0
    finally:
        owner_runtime.close()
        app_runtime.close()
