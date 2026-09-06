from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.modules.integrations.yandex import client_factory
from app.modules.integrations.yandex.client_factory import (
    DirectClientBindingNotFound,
    DirectClientBindingUnavailable,
    DirectClientClosed,
    DirectClientCredentialUnavailable,
    DirectClientLease,
    DirectClientLeaseInvalidated,
    DirectClientRefreshRequired,
    DirectClientRequest,
    PostgresConnectionScopedDirectClientFactory,
)
from app.modules.integrations.yandex.credentials import CredentialDecryptionError
from app.modules.tenancy.policy import AuthorizationDenied, Capability
from app.yandex_direct import YandexDirectClient


class _Authorizer:
    def __init__(self, *, deny_on_call: int | None = None) -> None:
        self._deny_on_call = deny_on_call
        self.calls: list[tuple[object, object, Capability]] = []

    def authorize(self, *, user_id, workspace_id, capability: Capability) -> None:
        self.calls.append((user_id, workspace_id, capability))
        if self._deny_on_call == len(self.calls):
            raise AuthorizationDenied("synthetic membership revocation")


class _Vault:
    def __init__(self, *, expires_at: datetime, routing_login: str | None = None) -> None:
        self._expires_at = expires_at
        self._routing_login = routing_login
        self.decrypt_calls = 0
        self.login_decrypt_calls = 0

    def decrypt(self, *, context, encrypted):
        del context, encrypted
        self.decrypt_calls += 1
        return SimpleNamespace(
            access_token="connection-credential-for-test",
            access_token_expires_at=self._expires_at,
        )

    def decrypt_provider_account_login(self, *, context, encrypted):
        del context, encrypted
        self.login_decrypt_calls += 1
        if self._routing_login is None:
            raise AssertionError("advertiser binding must not decrypt a routing Login")
        return self._routing_login


class _Session:
    def __init__(self, records: list[object]) -> None:
        self._records = iter(records)

    def scalar(self, statement):
        del statement
        return next(self._records)


@contextmanager
def _tenant_transaction(_sessions, *, workspace_id, user_id):
    del workspace_id, user_id
    yield _SESSION


def _binding_records(
    *,
    workspace_id,
    connection_id,
    provider_account_id,
    expires_at: datetime,
    connection_status: str = "active",
    account_status: str = "active",
    account_type: str = "advertiser",
    capabilities: list[str] | None = None,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    connection = SimpleNamespace(
        id=connection_id,
        workspace_id=workspace_id,
        provider="yandex",
        status=connection_status,
        version=7,
        token_ciphertext=b"a",
        token_nonce=b"b" * 12,
        wrapped_dek=b"c",
        wrap_nonce=b"d" * 12,
        kek_key_id="test-kek",
        schema_version=1,
        access_token_expires_at=expires_at,
        refresh_token_expires_at=None,
    )
    account = SimpleNamespace(
        id=provider_account_id,
        workspace_id=workspace_id,
        connection_id=connection_id,
        account_type=account_type,
        status=account_status,
        capabilities=["direct.read"] if capabilities is None else capabilities,
        version=11,
        login_ciphertext=b"e",
        login_nonce=b"f" * 12,
        login_wrapped_dek=b"g",
        login_wrap_nonce=b"h" * 12,
        login_kek_key_id="test-kek",
        login_schema_version=1,
    )
    return connection, account


def _factory(*, vault: _Vault) -> PostgresConnectionScopedDirectClientFactory:
    return PostgresConnectionScopedDirectClientFactory(
        sessions=object(),
        vault=vault,
        workspace_authorizer=_Authorizer(),
        settings=Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token=None),
    )


def test_active_advertiser_binding_uses_connection_credential_without_routing_headers(
    monkeypatch,
) -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    connection_id = uuid4()
    provider_account_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    connection = SimpleNamespace(
        id=connection_id,
        workspace_id=workspace_id,
        provider="yandex",
        status="active",
        version=7,
        token_ciphertext=b"a",
        token_nonce=b"b" * 12,
        wrapped_dek=b"c",
        wrap_nonce=b"d" * 12,
        kek_key_id="test-kek",
        schema_version=1,
        access_token_expires_at=expires_at,
        refresh_token_expires_at=None,
    )
    account = SimpleNamespace(
        id=provider_account_id,
        workspace_id=workspace_id,
        connection_id=connection_id,
        account_type="advertiser",
        status="active",
        capabilities=["direct.read"],
        version=11,
        login_ciphertext=b"e",
        login_nonce=b"f" * 12,
        login_wrapped_dek=b"g",
        login_wrap_nonce=b"h" * 12,
        login_kek_key_id="test-kek",
        login_schema_version=1,
    )
    global _SESSION
    _SESSION = _Session([connection, account, connection, account])
    monkeypatch.setattr(client_factory, "tenant_transaction", _tenant_transaction)

    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["client_login"] = request.headers.get("Client-Login")
        captured["operator_units"] = request.headers.get("Use-Operator-Units")
        return httpx.Response(200, json={"result": {}})

    authorizer = _Authorizer()
    vault = _Vault(expires_at=expires_at)
    factory = PostgresConnectionScopedDirectClientFactory(
        sessions=object(),
        vault=vault,
        workspace_authorizer=authorizer,
        settings=Settings(
            _env_file=None,
            directpilot_mode="sandbox",
            yandex_oauth_token="legacy-settings-sentinel",
        ),
        transport=httpx.MockTransport(handler),
    )

    lease = factory.create(
        request=DirectClientRequest(
            user_id=user_id,
            workspace_id=workspace_id,
            provider_connection_id=connection_id,
            provider_account_id=provider_account_id,
            request_id="test-request-1",
            runtime_mode="live_write",
        )
    )
    lease.client.clients_get()

    assert captured["authorization"] == "Bearer connection-credential-for-test"
    assert captured["client_login"] is None
    assert captured["operator_units"] is None
    assert authorizer.calls == [
        (user_id, workspace_id, Capability.READ_WORKSPACE_DATA),
        (user_id, workspace_id, Capability.READ_WORKSPACE_DATA),
    ]
    assert vault.decrypt_calls == 1
    assert vault.login_decrypt_calls == 0
    lease.close()


def test_agency_binding_applies_persisted_routing_login_to_json_and_reports(monkeypatch) -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    connection_id = uuid4()
    provider_account_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    connection = SimpleNamespace(
        id=connection_id,
        workspace_id=workspace_id,
        provider="yandex",
        status="active",
        version=7,
        token_ciphertext=b"a",
        token_nonce=b"b" * 12,
        wrapped_dek=b"c",
        wrap_nonce=b"d" * 12,
        kek_key_id="test-kek",
        schema_version=1,
        access_token_expires_at=expires_at,
        refresh_token_expires_at=None,
    )
    account = SimpleNamespace(
        id=provider_account_id,
        workspace_id=workspace_id,
        connection_id=connection_id,
        account_type="agency_client",
        status="active",
        capabilities=["direct.read"],
        version=11,
        login_ciphertext=b"e",
        login_nonce=b"f" * 12,
        login_wrapped_dek=b"g",
        login_wrap_nonce=b"h" * 12,
        login_kek_key_id="test-kek",
        login_schema_version=1,
    )
    global _SESSION
    _SESSION = _Session([connection, account, connection, account, connection, account])
    monkeypatch.setattr(client_factory, "tenant_transaction", _tenant_transaction)

    captured: list[tuple[str, str | None, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.url.path,
                request.headers.get("Authorization"),
                request.headers.get("Client-Login"),
                request.headers.get("Use-Operator-Units"),
            )
        )
        if request.url.path.endswith("/reports"):
            return httpx.Response(200, text="Date\tClicks\n")
        return httpx.Response(200, json={"result": {}})

    routing_login = "synthetic-routing-login"
    factory = PostgresConnectionScopedDirectClientFactory(
        sessions=object(),
        vault=_Vault(expires_at=expires_at, routing_login=routing_login),
        workspace_authorizer=_Authorizer(),
        settings=Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token=None),
        transport=httpx.MockTransport(handler),
    )

    lease = factory.create(
        request=DirectClientRequest(
            user_id=user_id,
            workspace_id=workspace_id,
            provider_connection_id=connection_id,
            provider_account_id=provider_account_id,
            request_id="test-request-2",
            runtime_mode="sandbox",
        )
    )
    lease.client.clients_get()
    lease.client.report(
        "CAMPAIGN_PERFORMANCE_REPORT",
        date_from="2026-01-01",
        date_to="2026-01-02",
    )

    assert len(captured) == 2
    assert all(item[1] == "Bearer connection-credential-for-test" for item in captured)
    assert all(item[2] == routing_login for item in captured)
    assert all(item[3] is None for item in captured)
    lease.close()


def test_connection_version_change_invalidates_lease_before_transport(monkeypatch) -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    connection_id = uuid4()
    provider_account_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    connection = SimpleNamespace(
        id=connection_id,
        workspace_id=workspace_id,
        provider="yandex",
        status="active",
        version=7,
        token_ciphertext=b"a",
        token_nonce=b"b" * 12,
        wrapped_dek=b"c",
        wrap_nonce=b"d" * 12,
        kek_key_id="test-kek",
        schema_version=1,
        access_token_expires_at=expires_at,
        refresh_token_expires_at=None,
    )
    account = SimpleNamespace(
        id=provider_account_id,
        workspace_id=workspace_id,
        connection_id=connection_id,
        account_type="advertiser",
        status="active",
        capabilities=["direct.read"],
        version=11,
        login_ciphertext=b"e",
        login_nonce=b"f" * 12,
        login_wrapped_dek=b"g",
        login_wrap_nonce=b"h" * 12,
        login_kek_key_id="test-kek",
        login_schema_version=1,
    )
    changed_connection = SimpleNamespace(**vars(connection))
    changed_connection.version += 1
    global _SESSION
    _SESSION = _Session([connection, account, changed_connection, account])
    monkeypatch.setattr(client_factory, "tenant_transaction", _tenant_transaction)

    transport_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal transport_calls
        transport_calls += 1
        return httpx.Response(200, json={"result": {}})

    factory = PostgresConnectionScopedDirectClientFactory(
        sessions=object(),
        vault=_Vault(expires_at=expires_at),
        workspace_authorizer=_Authorizer(),
        settings=Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token=None),
        transport=httpx.MockTransport(handler),
    )
    lease = factory.create(
        request=DirectClientRequest(
            user_id=user_id,
            workspace_id=workspace_id,
            provider_connection_id=connection_id,
            provider_account_id=provider_account_id,
            request_id="test-request-3",
            runtime_mode="live_readonly",
        )
    )

    with pytest.raises(DirectClientLeaseInvalidated):
        lease.client.clients_get()

    assert transport_calls == 0
    lease.close()


@pytest.mark.parametrize(
    ("connection_status", "account_status", "account_type", "capabilities"),
    [
        ("inactive", "active", "advertiser", ["direct.read"]),
        ("active", "archived", "advertiser", ["direct.read"]),
        ("active", "stale", "advertiser", ["direct.read"]),
        ("active", "active", "unsupported", ["direct.read"]),
        ("active", "active", "advertiser", []),
    ],
)
def test_unavailable_binding_fails_before_credential_decrypt(
    monkeypatch,
    connection_status: str,
    account_status: str,
    account_type: str,
    capabilities: list[str],
) -> None:
    workspace_id = uuid4()
    connection_id = uuid4()
    provider_account_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    connection, account = _binding_records(
        workspace_id=workspace_id,
        connection_id=connection_id,
        provider_account_id=provider_account_id,
        expires_at=expires_at,
        connection_status=connection_status,
        account_status=account_status,
        account_type=account_type,
        capabilities=capabilities,
    )
    global _SESSION
    _SESSION = _Session([connection, account])
    monkeypatch.setattr(client_factory, "tenant_transaction", _tenant_transaction)
    vault = _Vault(expires_at=expires_at)

    with pytest.raises(DirectClientBindingUnavailable):
        _factory(vault=vault).create(
            request=DirectClientRequest(
                user_id=uuid4(),
                workspace_id=workspace_id,
                provider_connection_id=connection_id,
                provider_account_id=provider_account_id,
                request_id="unavailable-binding-request",
                runtime_mode="sandbox",
            )
        )

    assert vault.decrypt_calls == 0
    assert vault.login_decrypt_calls == 0


def test_account_substitution_fails_before_credential_decrypt(monkeypatch) -> None:
    workspace_id = uuid4()
    connection_id = uuid4()
    provider_account_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    connection, _account = _binding_records(
        workspace_id=workspace_id,
        connection_id=connection_id,
        provider_account_id=provider_account_id,
        expires_at=expires_at,
    )
    global _SESSION
    _SESSION = _Session([connection, None])
    monkeypatch.setattr(client_factory, "tenant_transaction", _tenant_transaction)
    vault = _Vault(expires_at=expires_at)

    with pytest.raises(DirectClientBindingNotFound):
        _factory(vault=vault).create(
            request=DirectClientRequest(
                user_id=uuid4(),
                workspace_id=workspace_id,
                provider_connection_id=connection_id,
                provider_account_id=provider_account_id,
                request_id="account-substitution-request",
                runtime_mode="sandbox",
            )
        )

    assert vault.decrypt_calls == 0


def test_cross_connection_record_fails_before_credential_decrypt(monkeypatch) -> None:
    workspace_id = uuid4()
    connection_id = uuid4()
    provider_account_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    connection, account = _binding_records(
        workspace_id=workspace_id,
        connection_id=connection_id,
        provider_account_id=provider_account_id,
        expires_at=expires_at,
    )
    account.connection_id = uuid4()
    global _SESSION
    _SESSION = _Session([connection, account])
    monkeypatch.setattr(client_factory, "tenant_transaction", _tenant_transaction)
    vault = _Vault(expires_at=expires_at)

    with pytest.raises(DirectClientBindingNotFound):
        _factory(vault=vault).create(
            request=DirectClientRequest(
                user_id=uuid4(),
                workspace_id=workspace_id,
                provider_connection_id=connection_id,
                provider_account_id=provider_account_id,
                request_id="cross-connection-request",
                runtime_mode="sandbox",
            )
        )

    assert vault.decrypt_calls == 0


def test_expired_connection_credential_requires_lifecycle_refresh(monkeypatch) -> None:
    workspace_id = uuid4()
    connection_id = uuid4()
    provider_account_id = uuid4()
    expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    connection, account = _binding_records(
        workspace_id=workspace_id,
        connection_id=connection_id,
        provider_account_id=provider_account_id,
        expires_at=expires_at,
    )
    global _SESSION
    _SESSION = _Session([connection, account])
    monkeypatch.setattr(client_factory, "tenant_transaction", _tenant_transaction)
    vault = _Vault(expires_at=expires_at)

    with pytest.raises(DirectClientRefreshRequired):
        _factory(vault=vault).create(
            request=DirectClientRequest(
                user_id=uuid4(),
                workspace_id=workspace_id,
                provider_connection_id=connection_id,
                provider_account_id=provider_account_id,
                request_id="expired-credential-request",
                runtime_mode="sandbox",
            )
        )

    assert vault.decrypt_calls == 1


def test_credential_decrypt_failure_is_typed_and_token_free(monkeypatch) -> None:
    class UnavailableVault(_Vault):
        def decrypt(self, *, context, encrypted):
            del context, encrypted
            self.decrypt_calls += 1
            raise CredentialDecryptionError("synthetic vault failure")

    workspace_id = uuid4()
    connection_id = uuid4()
    provider_account_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    connection, account = _binding_records(
        workspace_id=workspace_id,
        connection_id=connection_id,
        provider_account_id=provider_account_id,
        expires_at=expires_at,
    )
    global _SESSION
    _SESSION = _Session([connection, account])
    monkeypatch.setattr(client_factory, "tenant_transaction", _tenant_transaction)
    vault = UnavailableVault(expires_at=expires_at)

    with pytest.raises(DirectClientCredentialUnavailable) as raised:
        _factory(vault=vault).create(
            request=DirectClientRequest(
                user_id=uuid4(),
                workspace_id=workspace_id,
                provider_connection_id=connection_id,
                provider_account_id=provider_account_id,
                request_id="credential-failure-request",
                runtime_mode="sandbox",
            )
        )

    assert "synthetic vault failure" not in str(raised.value)


def test_agency_unit_switch_is_rejected_at_request_boundary() -> None:
    with pytest.raises(ValueError):
        DirectClientRequest(
            user_id=uuid4(),
            workspace_id=uuid4(),
            provider_connection_id=uuid4(),
            provider_account_id=uuid4(),
            request_id="agency-unit-request",
            runtime_mode="sandbox",
            spend_agency_units=True,
        )


def test_lease_context_manager_closes_client_and_rejects_post_close_access() -> None:
    client = YandexDirectClient(
        settings=Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token=None),
        access_token="connection-credential-for-test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"result": {}})),
    )
    lease = DirectClientLease(
        client=client,
        connection_id=uuid4(),
        provider_account_id=uuid4(),
        request_id="context-manager-request",
    )

    with lease as leased_client:
        assert leased_client is client

    with pytest.raises(DirectClientClosed):
        _ = lease.client


@pytest.mark.parametrize(
    "mutation",
    [
        "connection_missing",
        "connection_status",
        "account_status",
        "account_capability",
        "account_version",
        "membership_revoked",
    ],
)
def test_lease_recheck_blocks_invalidated_binding_before_transport(monkeypatch, mutation: str) -> None:
    workspace_id = uuid4()
    user_id = uuid4()
    connection_id = uuid4()
    provider_account_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    connection, account = _binding_records(
        workspace_id=workspace_id,
        connection_id=connection_id,
        provider_account_id=provider_account_id,
        expires_at=expires_at,
    )
    records: list[object] = [connection, account]
    authorizer = _Authorizer(deny_on_call=2 if mutation == "membership_revoked" else None)
    if mutation != "membership_revoked":
        rechecked_connection = SimpleNamespace(**vars(connection))
        rechecked_account = SimpleNamespace(**vars(account))
        if mutation == "connection_missing":
            rechecked_connection = None
        elif mutation == "connection_status":
            rechecked_connection.status = "inactive"
        elif mutation == "account_status":
            rechecked_account.status = "archived"
        elif mutation == "account_capability":
            rechecked_account.capabilities = []
        elif mutation == "account_version":
            rechecked_account.version += 1
        records.extend([rechecked_connection, rechecked_account])
    global _SESSION
    _SESSION = _Session(records)
    monkeypatch.setattr(client_factory, "tenant_transaction", _tenant_transaction)

    transport_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal transport_calls
        transport_calls += 1
        return httpx.Response(200, json={"result": {}})

    factory = PostgresConnectionScopedDirectClientFactory(
        sessions=object(),
        vault=_Vault(expires_at=expires_at),
        workspace_authorizer=authorizer,
        settings=Settings(_env_file=None, directpilot_mode="live_write", yandex_oauth_token=None),
        transport=httpx.MockTransport(handler),
    )
    lease = factory.create(
        request=DirectClientRequest(
            user_id=user_id,
            workspace_id=workspace_id,
            provider_connection_id=connection_id,
            provider_account_id=provider_account_id,
            request_id=f"lease-{mutation}",
            runtime_mode="live_write",
        )
    )

    with pytest.raises(DirectClientLeaseInvalidated):
        lease.client.clients_get()

    assert transport_calls == 0
    lease.close()
