from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import select

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.models import AuditEventRecord, YandexProviderConnectionRecord
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
from app.modules.integrations.yandex.oauth import YandexOAuthTokenSet
from app.modules.integrations.yandex.refresh import YandexConnectionLifecycleService
from app.modules.integrations.yandex.repository import (
    PostgresExternalIdentityRepository,
    PostgresYandexProviderConnectionRepository,
)


class _RecordingRefreshProvider:
    def __init__(
        self,
        *,
        expected_refresh_token: str,
        access_token: str,
        returned_refresh_token: str,
    ) -> None:
        self._expected_refresh_token = expected_refresh_token
        self._access_token = access_token
        self._returned_refresh_token = returned_refresh_token
        self.calls = 0

    def refresh_tokens(self, *, refresh_token: str) -> YandexOAuthTokenSet:
        assert refresh_token == self._expected_refresh_token
        self.calls += 1
        return YandexOAuthTokenSet(
            access_token=self._access_token,
            refresh_token=self._returned_refresh_token,
            expires_in=1800,
        )


def _vault() -> CredentialVault:
    return CredentialVault(
        CredentialKeyRing.from_keys(
            active_key_id="test-kek-v1",
            keys={"test-kek-v1": b"k" * 32},
        )
    )


def _decrypt(
    *,
    vault: CredentialVault,
    workspace_id: UUID,
    record: YandexProviderConnectionRecord,
) -> YandexCredentialPayload:
    return vault.decrypt(
        context=CredentialContext.for_yandex_connection(
            workspace_id=workspace_id,
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


@pytest.mark.integration
def test_refresh_reencrypts_the_pair_and_bumps_version_when_access_token_is_unchanged(
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
    now = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
    access_token = "synthetic-unchanged-access-token"
    old_refresh_token = "synthetic-old-refresh-token"
    new_refresh_token = "synthetic-new-refresh-token"
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        identity = PostgresIdentityRepository(app_runtime.sessions).create_personal_workspace(
            display_name="Synthetic refresh owner",
            workspace_name="Synthetic refresh workspace",
        )
        subject = "synthetic-refresh-subject"
        PostgresExternalIdentityRepository(app_runtime.sessions).bind_yandex_identity(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            issuer="https://login.yandex.ru",
            subject=subject,
            profile_login="synthetic-refresh-login",
            profile_display_name="Synthetic refresh identity",
            now=now,
        )
        vault = _vault()
        connections = PostgresYandexProviderConnectionRepository(app_runtime.sessions, vault=vault)
        connection = connections.persist_yandex_oauth_tokens(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            issuer="https://login.yandex.ru",
            subject=subject,
            payload=YandexCredentialPayload(
                access_token=access_token,
                refresh_token=old_refresh_token,
                access_token_expires_at=now + timedelta(seconds=60),
                refresh_token_expires_at=None,
            ),
            now=now,
        )
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=identity.workspace.id,
            user_id=identity.user.id,
        ) as session:
            before = session.scalar(
                select(YandexProviderConnectionRecord).where(
                    YandexProviderConnectionRecord.id == connection.id
                )
            )
            assert before is not None
            old_ciphertext = before.token_ciphertext
            old_wrapped_dek = before.wrapped_dek
        provider = _RecordingRefreshProvider(
            expected_refresh_token=old_refresh_token,
            access_token=access_token,
            returned_refresh_token=new_refresh_token,
        )
        lifecycle = YandexConnectionLifecycleService(
            repository=connections,
            provider=provider,
            refresh_skew_seconds=60,
        )

        result = lifecycle.refresh(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            connection_id=connection.id,
            now=now,
        )

        assert provider.calls == 1
        assert result.connection_id == connection.id
        assert result.status == "active"
        assert result.refreshed is True
        assert result.credential_version == 2
        assert result.access_token_expires_at == now + timedelta(seconds=1800)
        assert access_token not in repr(result)
        assert old_refresh_token not in repr(result)
        assert new_refresh_token not in repr(result)
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=identity.workspace.id,
            user_id=identity.user.id,
        ) as session:
            after = session.scalar(
                select(YandexProviderConnectionRecord).where(
                    YandexProviderConnectionRecord.id == connection.id
                )
            )
            audit_events = session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.workspace_id == identity.workspace.id
                )
            ).all()
        assert after is not None
        assert after.version == 2
        assert after.token_ciphertext != old_ciphertext
        assert after.wrapped_dek != old_wrapped_dek
        assert _decrypt(vault=vault, workspace_id=identity.workspace.id, record=after) == (
            YandexCredentialPayload(
                access_token=access_token,
                refresh_token=new_refresh_token,
                access_token_expires_at=now + timedelta(seconds=1800),
                refresh_token_expires_at=now + timedelta(seconds=1800),
            )
        )
        assert len(audit_events) == 1
        assert audit_events[0].action == "yandex_connection_refresh"
        assert audit_events[0].details == {"outcome": "refreshed", "provider": "yandex"}
    finally:
        owner_runtime.close()
        app_runtime.close()


class _NoRefreshProvider:
    def __init__(self) -> None:
        self.calls = 0

    def refresh_tokens(self, *, refresh_token: str) -> YandexOAuthTokenSet:
        del refresh_token
        self.calls += 1
        raise AssertionError("disconnect must not invoke a provider refresh")


@pytest.mark.integration
def test_disconnect_hard_deletes_atomically_without_any_provider_call_and_is_repeat_safe(
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
    now = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        identity = PostgresIdentityRepository(app_runtime.sessions).create_personal_workspace(
            display_name="Synthetic disconnect owner",
            workspace_name="Synthetic disconnect workspace",
        )
        subject = "synthetic-disconnect-subject"
        PostgresExternalIdentityRepository(app_runtime.sessions).bind_yandex_identity(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            issuer="https://login.yandex.ru",
            subject=subject,
            profile_login="synthetic-disconnect-login",
            profile_display_name="Synthetic disconnect identity",
            now=now,
        )
        connections = PostgresYandexProviderConnectionRepository(
            app_runtime.sessions,
            vault=_vault(),
        )
        connection = connections.persist_yandex_oauth_tokens(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            issuer="https://login.yandex.ru",
            subject=subject,
            payload=YandexCredentialPayload(
                access_token="synthetic-disconnect-access-token",
                refresh_token="synthetic-disconnect-refresh-token",
                access_token_expires_at=now + timedelta(hours=1),
                refresh_token_expires_at=None,
            ),
            now=now,
        )
        provider = _NoRefreshProvider()
        lifecycle = YandexConnectionLifecycleService(
            repository=connections,
            provider=provider,
        )

        result = lifecycle.disconnect(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            connection_id=connection.id,
            now=now,
        )

        assert result.connection_id == connection.id
        assert result.status == "disconnected"
        assert result.local_credentials_purged is True
        assert result.provider_revocation == "not_supported_for_current_grant"
        assert result.yandex_revocation_url == "https://id.yandex.ru/personal/data-access"
        assert provider.calls == 0
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=identity.workspace.id,
            user_id=identity.user.id,
        ) as session:
            record = session.scalar(
                select(YandexProviderConnectionRecord).where(
                    YandexProviderConnectionRecord.id == connection.id
                )
            )
            audit_events = session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.workspace_id == identity.workspace.id
                )
            ).all()
        assert record is None
        assert len(audit_events) == 1
        assert audit_events[0].action == "yandex_connection_disconnect"
        assert audit_events[0].details == {"outcome": "disconnect_local", "provider": "yandex"}

        repeated = lifecycle.disconnect(
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            connection_id=connection.id,
            now=now,
        )

        assert repeated == result
        assert provider.calls == 0
    finally:
        owner_runtime.close()
        app_runtime.close()
