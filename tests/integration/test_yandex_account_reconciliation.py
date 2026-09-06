from __future__ import annotations

from datetime import datetime, timezone
from typing import NoReturn
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.models import ProviderAccountRecord, YandexProviderConnectionRecord
from app.db.rls import tenant_transaction
from app.db.roles import bootstrap_database_roles
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.integrations.yandex.account_provider import (
    DirectAccountDiscoveryPage,
    HttpxYandexDirectAccountProvider,
)
from app.modules.integrations.yandex.accounts import (
    AccountDiscoveryFailure,
    AccountDiscoveryService,
    ValidatedProviderAccount,
)
from app.modules.integrations.yandex.credentials import (
    CredentialKeyRing,
    CredentialVault,
    YandexCredentialPayload,
)
from app.modules.integrations.yandex.provider import YandexOAuthProviderFailure
from app.modules.integrations.yandex.refresh import (
    YandexConnectionLifecycleService,
    YandexConnectionReauthorizationRequired,
)
from app.modules.integrations.yandex.repository import (
    PostgresExternalIdentityRepository,
    PostgresYandexProviderAccountRepository,
    PostgresYandexProviderConnectionRepository,
    ProviderAccountDiscoveryUnavailable,
)

_NOW = datetime(2026, 9, 6, 15, tzinfo=timezone.utc)


class _Provider:
    def __init__(self, holder_page: DirectAccountDiscoveryPage) -> None:
        self._holder_page = holder_page
        self.calls = 0

    def clients_get(self) -> DirectAccountDiscoveryPage:
        self.calls += 1
        return self._holder_page

    def agencyclients_get(self, *, offset: int) -> DirectAccountDiscoveryPage:
        raise AssertionError(f"unexpected agency page request: {offset}")


class _NoRefreshProvider:
    def __init__(self) -> None:
        self.calls = 0

    def refresh_tokens(self, *, refresh_token: str) -> NoReturn:
        del refresh_token
        self.calls += 1
        raise AssertionError("disconnect must not refresh a provider connection")


class _InvalidGrantRefreshProvider:
    def __init__(self) -> None:
        self.calls = 0

    def refresh_tokens(self, *, refresh_token: str) -> NoReturn:
        del refresh_token
        self.calls += 1
        raise YandexOAuthProviderFailure("invalid_grant")


def _row(client_id: int, login: str) -> dict[str, object]:
    return {
        "ClientId": client_id,
        "Login": login,
        "ClientInfo": f"Synthetic {client_id}",
        "Type": "CLIENT",
        "Archived": "NO",
        "CountryId": 225,
        "Currency": "RUB",
        "Grants": [{"Privilege": "EDIT_CAMPAIGNS", "Value": "YES"}],
        "Representatives": [{"Role": "CHIEF"}],
    }


def _ordinary_provider(client_id: int, login: str) -> _Provider:
    return _Provider(DirectAccountDiscoveryPage(client_rows=(_row(client_id, login),)))


@pytest.mark.integration
def test_reconciliation_preserves_failed_inventory_and_enforces_rls_uniqueness_and_version(
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
        identities = PostgresIdentityRepository(app_runtime.sessions)
        owner = identities.create_personal_workspace(
            display_name="Synthetic reconciliation owner",
            workspace_name="Synthetic reconciliation workspace",
        )
        identity_repository = PostgresExternalIdentityRepository(app_runtime.sessions)
        identity_repository.bind_yandex_identity(
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            issuer="https://login.yandex.ru",
            subject="synthetic-reconciliation-subject",
            profile_login="synthetic-reconciliation-profile",
            profile_display_name="Synthetic reconciliation profile",
            now=_NOW,
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
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            issuer="https://login.yandex.ru",
            subject="synthetic-reconciliation-subject",
            payload=YandexCredentialPayload(
                access_token="synthetic-access-token",
                refresh_token="synthetic-refresh-token",
                access_token_expires_at=_NOW,
                refresh_token_expires_at=None,
            ),
            now=_NOW,
        )
        repository = PostgresYandexProviderAccountRepository(app_runtime.sessions, vault=vault)
        service = AccountDiscoveryService(repository=repository)

        first = service.discover(
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            connection_id=connection.id,
            provider=_ordinary_provider(701, "synthetic-routing-login-one"),
            now=_NOW,
        )
        second = service.discover(
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            connection_id=connection.id,
            provider=_ordinary_provider(702, "synthetic-routing-login-two"),
            now=_NOW,
        )

        assert len(first) == 1
        assert len(second) == 1
        assert first[0].id != second[0].id
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=owner.workspace.id,
            user_id=owner.user.id,
        ) as session:
            records_before_failure = {
                record.provider_account_key: record.status
                for record in session.scalars(
                    select(ProviderAccountRecord).order_by(ProviderAccountRecord.provider_account_key)
                )
            }
        assert records_before_failure == {"701": "stale", "702": "active"}

        malformed_provider = _Provider(
            DirectAccountDiscoveryPage(
                client_rows=(
                    _row(703, "synthetic-malformed-one"),
                    _row(704, "synthetic-malformed-two"),
                )
            )
        )
        with pytest.raises(AccountDiscoveryFailure):
            service.discover(
                user_id=owner.user.id,
                workspace_id=owner.workspace.id,
                connection_id=connection.id,
                provider=malformed_provider,
                now=_NOW,
            )
        assert malformed_provider.calls == 1
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=owner.workspace.id,
            user_id=owner.user.id,
        ) as session:
            assert {
                record.provider_account_key: record.status
                for record in session.scalars(
                    select(ProviderAccountRecord).order_by(ProviderAccountRecord.provider_account_key)
                )
            } == records_before_failure

        call_paths: list[str] = []

        def null_limited_by_handler(request: httpx.Request) -> httpx.Response:
            call_paths.append(request.url.path)
            if request.url.path.endswith("/clients"):
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "Clients": [
                                {
                                    "ClientId": 799,
                                    "Login": "synthetic-null-limited-by-agency-login",
                                    "ClientInfo": "Synthetic null limited by agency",
                                    "Type": "AGENCY",
                                    "Archived": "NO",
                                }
                            ]
                        }
                    },
                )
            agency_row = _row(703, "synthetic-null-limited-by-client-login")
            agency_row["Type"] = "SUBCLIENT"
            return httpx.Response(
                200,
                json={"result": {"Clients": [agency_row], "LimitedBy": None}},
            )

        null_limited_by_provider = HttpxYandexDirectAccountProvider(
            access_token="synthetic-null-limited-by-access-token",
            transport=httpx.MockTransport(null_limited_by_handler),
            sleeper=lambda _: None,
        )
        with pytest.raises(AccountDiscoveryFailure) as null_limited_by_failure:
            service.discover(
                user_id=owner.user.id,
                workspace_id=owner.workspace.id,
                connection_id=connection.id,
                provider=null_limited_by_provider,
                now=_NOW,
            )
        assert null_limited_by_failure.value.kind == "provider_malformed_response"
        assert call_paths == ["/json/v5/clients", "/json/v5/agencyclients"]
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=owner.workspace.id,
            user_id=owner.user.id,
        ) as session:
            assert {
                record.provider_account_key: record.status
                for record in session.scalars(
                    select(ProviderAccountRecord).order_by(ProviderAccountRecord.provider_account_key)
                )
            } == records_before_failure

        non_rfc_call_paths: list[str] = []
        raw_marker = "synthetic-non-rfc-integration-login"
        non_rfc_body = (
            '{"result":{"Clients":[{"ClientId":705,"Login":"'
            + raw_marker
            + '","ClientInfo":"Synthetic non-RFC integration account","Type":"CLIENT",'
            '"Archived":"NO","Grants":NaN}]}}'
        ).encode("utf-8")

        def non_rfc_handler(request: httpx.Request) -> httpx.Response:
            non_rfc_call_paths.append(request.url.path)
            return httpx.Response(200, content=non_rfc_body)

        non_rfc_provider = HttpxYandexDirectAccountProvider(
            access_token="synthetic-non-rfc-integration-access-token",
            transport=httpx.MockTransport(non_rfc_handler),
            sleeper=lambda _: None,
        )
        with pytest.raises(AccountDiscoveryFailure) as non_rfc_failure:
            service.discover(
                user_id=owner.user.id,
                workspace_id=owner.workspace.id,
                connection_id=connection.id,
                provider=non_rfc_provider,
                now=_NOW,
            )
        assert non_rfc_failure.value.kind == "provider_malformed_response"
        assert non_rfc_call_paths == ["/json/v5/clients"]

        def non_rfc_failure_is_redacted() -> bool:
            return (
                raw_marker not in str(non_rfc_failure.value)
                and raw_marker not in repr(non_rfc_failure.value)
            )

        assert non_rfc_failure_is_redacted() is True
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=owner.workspace.id,
            user_id=owner.user.id,
        ) as session:
            assert {
                record.provider_account_key: record.status
                for record in session.scalars(
                    select(ProviderAccountRecord).order_by(ProviderAccountRecord.provider_account_key)
                )
            } == records_before_failure

        first_version = repository.begin_discovery(
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            connection_id=connection.id,
        )
        second_version = repository.begin_discovery(
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            connection_id=connection.id,
        )
        assert first_version == second_version
        concurrent_candidate = ValidatedProviderAccount(
            provider_account_key="703",
            routing_login="synthetic-concurrent-routing-login",
            display_name="Synthetic concurrent account",
            account_type="advertiser",
            status="active",
            country_id=225,
            currency="RUB",
        )
        repository.reconcile_discovery(
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            connection_id=connection.id,
            expected_connection_version=first_version,
            candidates=(concurrent_candidate,),
            now=_NOW,
        )
        with pytest.raises(ProviderAccountDiscoveryUnavailable):
            repository.reconcile_discovery(
                user_id=owner.user.id,
                workspace_id=owner.workspace.id,
                connection_id=connection.id,
                expected_connection_version=second_version,
                candidates=(concurrent_candidate,),
                now=_NOW,
            )

        with pytest.raises(IntegrityError):
            with tenant_transaction(
                app_runtime.sessions,
                workspace_id=owner.workspace.id,
                user_id=owner.user.id,
            ) as session:
                original = session.scalar(
                    select(ProviderAccountRecord).where(
                        ProviderAccountRecord.provider_account_key == "703"
                    )
                )
                assert original is not None
                session.add(
                    ProviderAccountRecord(
                        id=uuid4(),
                        workspace_id=original.workspace_id,
                        connection_id=original.connection_id,
                        provider_account_key=original.provider_account_key,
                        account_type=original.account_type,
                        display_name=original.display_name,
                        status=original.status,
                        capabilities=original.capabilities,
                        country_id=original.country_id,
                        currency=original.currency,
                        login_ciphertext=original.login_ciphertext,
                        login_nonce=original.login_nonce,
                        login_wrapped_dek=original.login_wrapped_dek,
                        login_wrap_nonce=original.login_wrap_nonce,
                        login_kek_key_id=original.login_kek_key_id,
                        login_schema_version=original.login_schema_version,
                        last_discovered_at=original.last_discovered_at,
                        last_verified_at=original.last_verified_at,
                        created_at=original.created_at,
                        updated_at=original.updated_at,
                        version=1,
                    )
                )
                session.flush()

        other = identities.create_personal_workspace(
            display_name="Synthetic isolated user",
            workspace_name="Synthetic isolated workspace",
        )
        blocked_provider = _ordinary_provider(704, "synthetic-cross-workspace-login")
        with pytest.raises(ProviderAccountDiscoveryUnavailable):
            service.discover(
                user_id=other.user.id,
                workspace_id=other.workspace.id,
                connection_id=connection.id,
                provider=blocked_provider,
                now=_NOW,
            )
        assert blocked_provider.calls == 0
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=other.workspace.id,
            user_id=other.user.id,
        ) as session:
            assert session.scalar(select(ProviderAccountRecord.id)) is None

        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=owner.workspace.id,
            user_id=owner.user.id,
        ) as session:
            active_connection = session.scalar(
                select(YandexProviderConnectionRecord)
                .where(YandexProviderConnectionRecord.id == connection.id)
                .with_for_update()
            )
            assert active_connection is not None
            active_connection.status = "reauth_required"
            session.flush()
        disabled_provider = _ordinary_provider(705, "synthetic-disabled-connection-login")
        with pytest.raises(ProviderAccountDiscoveryUnavailable):
            service.discover(
                user_id=owner.user.id,
                workspace_id=owner.workspace.id,
                connection_id=connection.id,
                provider=disabled_provider,
                now=_NOW,
            )
        assert disabled_provider.calls == 0
        with tenant_transaction(
            app_runtime.sessions,
            workspace_id=owner.workspace.id,
            user_id=owner.user.id,
        ) as session:
            assert session.scalar(select(ProviderAccountRecord.id)) is None
    finally:
        owner_runtime.close()
        app_runtime.close()


@pytest.mark.integration
@pytest.mark.parametrize("lifecycle_operation", ["disconnect", "invalid_grant"])
def test_discovered_accounts_cascade_when_connection_lifecycle_deletes_parent(
    postgres_service: object,
    lifecycle_operation: str,
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
        owner = PostgresIdentityRepository(app_runtime.sessions).create_personal_workspace(
            display_name="Synthetic lifecycle owner",
            workspace_name="Synthetic lifecycle workspace",
        )
        subject = "synthetic-lifecycle-subject"
        PostgresExternalIdentityRepository(app_runtime.sessions).bind_yandex_identity(
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            issuer="https://login.yandex.ru",
            subject=subject,
            profile_login="synthetic-lifecycle-profile",
            profile_display_name="Synthetic lifecycle profile",
            now=_NOW,
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
        connection = connections.persist_yandex_oauth_tokens(
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            issuer="https://login.yandex.ru",
            subject=subject,
            payload=YandexCredentialPayload(
                access_token="synthetic-lifecycle-access-token",
                refresh_token="synthetic-lifecycle-refresh-token",
                access_token_expires_at=_NOW,
                refresh_token_expires_at=None,
            ),
            now=_NOW,
        )
        discovered = AccountDiscoveryService(
            repository=PostgresYandexProviderAccountRepository(app_runtime.sessions, vault=vault)
        ).discover(
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            connection_id=connection.id,
            provider=_ordinary_provider(811, "synthetic-lifecycle-routing-login"),
            now=_NOW,
        )
        assert len(discovered) == 1

        if lifecycle_operation == "disconnect":
            provider = _NoRefreshProvider()
            result = YandexConnectionLifecycleService(
                repository=connections,
                provider=provider,
            ).disconnect(
                user_id=owner.user.id,
                workspace_id=owner.workspace.id,
                connection_id=connection.id,
                now=_NOW,
            )
            assert result.local_credentials_purged is True
            assert provider.calls == 0
        else:
            provider = _InvalidGrantRefreshProvider()
            with pytest.raises(YandexConnectionReauthorizationRequired):
                YandexConnectionLifecycleService(
                    repository=connections,
                    provider=provider,
                    refresh_skew_seconds=0,
                ).refresh(
                    user_id=owner.user.id,
                    workspace_id=owner.workspace.id,
                    connection_id=connection.id,
                    now=_NOW,
                )
            assert provider.calls == 1

        with owner_runtime.sessions() as session:
            assert session.scalar(
                select(YandexProviderConnectionRecord.id).where(
                    YandexProviderConnectionRecord.id == connection.id
                )
            ) is None
            assert session.scalar(
                select(ProviderAccountRecord.id).where(
                    ProviderAccountRecord.connection_id == connection.id
                )
            ) is None
    finally:
        owner_runtime.close()
        app_runtime.close()
