from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.models import ProviderAccountRecord
from app.db.rls import tenant_transaction
from app.db.roles import bootstrap_database_roles
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.integrations.yandex.account_provider import DirectAccountDiscoveryPage
from app.modules.integrations.yandex.accounts import AccountDiscoveryService
from app.modules.integrations.yandex.credentials import (
    CredentialKeyRing,
    CredentialVault,
    CredentialVaultError,
    EncryptedProviderAccountLogin,
    ProviderAccountLoginContext,
    YandexCredentialPayload,
)
from app.modules.integrations.yandex.repository import (
    PostgresExternalIdentityRepository,
    PostgresYandexProviderAccountRepository,
    PostgresYandexProviderConnectionRepository,
)


class _OrdinaryProvider:
    def __init__(self, *, client_info: object = "Synthetic Provider Account") -> None:
        self._client_info = client_info

    def clients_get(self) -> DirectAccountDiscoveryPage:
        return DirectAccountDiscoveryPage(
            client_rows=(
                {
                    "ClientId": 123456789,
                    "Login": "synthetic-provider-routing-login",
                    "ClientInfo": self._client_info,
                    "Type": "CLIENT",
                    "Archived": "NO",
                    "CountryId": 225,
                    "Currency": "RUB",
                    "Grants": [{"Privilege": "EDIT_CAMPAIGNS", "Value": "YES"}],
                    "Representatives": [{"Role": "CHIEF"}],
                },
            )
        )

    def agencyclients_get(self, *, offset: int) -> DirectAccountDiscoveryPage:
        raise AssertionError(f"unexpected agency discovery: {offset}")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("client_info", "expected_display_name"),
    [
        ("Synthetic Provider Account", "Synthetic Provider Account"),
        (None, "Yandex Direct account 123456789"),
        (" \t", "Yandex Direct account 123456789"),
    ],
)
def test_discovery_persists_a_protected_login_and_returns_only_safe_account_data(
    postgres_service: object,
    client_info: object,
    expected_display_name: str,
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
    login = "synthetic-provider-routing-login"
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        identity = PostgresIdentityRepository(app_runtime.sessions).create_personal_workspace(
            display_name="Synthetic account owner",
            workspace_name="Synthetic account workspace",
        )
        PostgresExternalIdentityRepository(app_runtime.sessions).bind_yandex_identity(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            issuer="https://login.yandex.ru",
            subject="synthetic-account-subject",
            profile_login="synthetic-profile-login",
            profile_display_name="Synthetic profile name",
            now=now,
        )
        vault = CredentialVault(
            CredentialKeyRing.from_keys(
                active_key_id="test-kek-v1",
                keys={"test-kek-v1": b"k" * 32},
            )
        )
        connection = PostgresYandexProviderConnectionRepository(
            app_runtime.sessions,
            vault=vault,
        ).persist_yandex_oauth_tokens(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            issuer="https://login.yandex.ru",
            subject="synthetic-account-subject",
            payload=YandexCredentialPayload(
                access_token="synthetic-access-token",
                refresh_token="synthetic-refresh-token",
                access_token_expires_at=now,
                refresh_token_expires_at=None,
            ),
            now=now,
        )

        result = AccountDiscoveryService(
            repository=PostgresYandexProviderAccountRepository(app_runtime.sessions, vault=vault)
        ).discover(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            connection_id=connection.id,
            provider=_OrdinaryProvider(client_info=client_info),
            now=now,
        )

        assert len(result) == 1
        safe_account = result[0]
        assert safe_account.workspace_id == identity.workspace.id
        assert safe_account.connection_id == connection.id
        assert safe_account.account_type == "advertiser"
        assert safe_account.status == "active"
        assert safe_account.capabilities == ("direct.read",)
        assert "direct.edit" not in safe_account.capabilities
        assert _repr_excludes_login(value=safe_account, raw_login=login) is True
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=identity.workspace.id,
            user_id=identity.user.id,
        ) as session:
            record = session.scalar(select(ProviderAccountRecord))
        assert record is not None
        assert record.provider_account_key == "123456789"
        assert record.login_ciphertext != login.encode()
        assert record.capabilities == ["direct.read"]
        encrypted = EncryptedProviderAccountLogin(
            ciphertext=record.login_ciphertext,
            nonce=record.login_nonce,
            wrapped_dek=record.login_wrapped_dek,
            wrap_nonce=record.login_wrap_nonce,
            kek_key_id=record.login_kek_key_id,
            schema_version=record.login_schema_version,
        )
        context = ProviderAccountLoginContext.for_yandex_provider_account(
            workspace_id=identity.workspace.id,
            connection_id=connection.id,
            provider_account_id=record.id,
        )
        assert _account_output_is_safe(
            safe_account=safe_account,
            record=record,
            expected_display_name=expected_display_name,
            raw_login=login,
        ) is True
        assert _provider_login_decrypts_only_with_correct_context(
            vault=vault,
            context=context,
            encrypted=encrypted,
            raw_login=login,
        )
        for mismatched_context in (
            replace(context, workspace_id=uuid4()),
            replace(context, connection_id=uuid4()),
            replace(context, provider_account_id=uuid4()),
        ):
            with pytest.raises(CredentialVaultError):
                vault.decrypt_provider_account_login(
                    context=mismatched_context,
                    encrypted=encrypted,
                )
    finally:
        owner_runtime.close()
        app_runtime.close()


def _account_output_is_safe(
    *,
    safe_account: object,
    record: ProviderAccountRecord,
    expected_display_name: str,
    raw_login: str,
) -> bool:
    persisted_fields = {column.name: getattr(record, column.name) for column in record.__table__.columns}
    return (
        getattr(safe_account, "display_name", None) == expected_display_name
        and record.display_name == expected_display_name
        and all(raw_login not in repr(value) for value in (safe_account, record, persisted_fields))
        and raw_login not in record.display_name
    )


def _repr_excludes_login(*, value: object, raw_login: str) -> bool:
    return raw_login not in repr(value)


def _provider_login_decrypts_only_with_correct_context(
    *,
    vault: CredentialVault,
    context: ProviderAccountLoginContext,
    encrypted: EncryptedProviderAccountLogin,
    raw_login: str,
) -> bool:
    return vault.decrypt_provider_account_login(context=context, encrypted=encrypted) == raw_login
