from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.yandex.account_provider import (
    DirectAccountDiscoveryPage,
    HttpxYandexDirectAccountProvider,
)
from app.modules.integrations.yandex.accounts import (
    AccountDiscoveryFailure,
    AccountDiscoveryService,
    DiscoveredProviderAccount,
    ValidatedProviderAccount,
)

_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000011")
_CONNECTION_ID = UUID("00000000-0000-4000-8000-000000000012")
_USER_ID = UUID("00000000-0000-4000-8000-000000000013")
_NOW = datetime(2026, 9, 6, 13, tzinfo=timezone.utc)


class _OrdinaryProvider:
    def __init__(
        self,
        *,
        client_info: object = "Synthetic Holder",
        login: str = "synthetic-holder-login",
    ) -> None:
        self._client_info = client_info
        self._login = login

    def clients_get(self) -> DirectAccountDiscoveryPage:
        return DirectAccountDiscoveryPage(
            client_rows=(
                {
                    "ClientId": 123456789,
                    "Login": self._login,
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
        raise AssertionError(f"ordinary discovery must not enumerate agency clients: {offset}")


class _RecordingRepository:
    def __init__(self) -> None:
        self.begin_calls: list[tuple[UUID, UUID, UUID]] = []
        self.reconcile_calls: list[tuple[object, ...]] = []

    def begin_discovery(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
    ) -> int:
        self.begin_calls.append((user_id, workspace_id, connection_id))
        return 7

    def reconcile_discovery(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        expected_connection_version: int,
        candidates: tuple[ValidatedProviderAccount, ...],
        now: datetime,
    ) -> tuple[DiscoveredProviderAccount, ...]:
        self.reconcile_calls.append(
            (
                user_id,
                workspace_id,
                connection_id,
                expected_connection_version,
                candidates,
                now,
            )
        )
        return ()


def test_client_discovery_normalizes_one_read_only_account_without_login_in_repr() -> None:
    repository = _RecordingRepository()
    service = AccountDiscoveryService(repository=repository)

    result = service.discover(
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        connection_id=_CONNECTION_ID,
        provider=_OrdinaryProvider(),
        now=_NOW,
    )

    assert result == ()
    assert repository.begin_calls == [(_USER_ID, _WORKSPACE_ID, _CONNECTION_ID)]
    assert len(repository.reconcile_calls) == 1
    _, _, _, version, candidates, now = repository.reconcile_calls[0]
    assert version == 7
    assert now == _NOW
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.provider_account_key == "123456789"
    assert candidate.display_name == "Synthetic Holder"
    assert candidate.account_type == "advertiser"
    assert candidate.status == "active"
    assert candidate.country_id == 225
    assert candidate.currency == "RUB"
    assert candidate.capabilities == ("direct.read",)
    assert "direct.edit" not in candidate.capabilities
    assert "synthetic-holder-login" not in repr(candidate)


@pytest.mark.parametrize("client_info", [None, " \t"])
def test_client_discovery_uses_non_sensitive_label_when_client_info_missing_or_blank(
    client_info: object,
) -> None:
    repository = _RecordingRepository()
    raw_login = "synthetic-login-that-must-not-display"

    AccountDiscoveryService(repository=repository).discover(
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        connection_id=_CONNECTION_ID,
        provider=_OrdinaryProvider(client_info=client_info, login=raw_login),
        now=_NOW,
    )

    _, _, _, _, candidates, _ = repository.reconcile_calls[0]
    assert _candidate_has_safe_fallback(candidate=candidates[0], raw_login=raw_login) is True


def _candidate_has_safe_fallback(*, candidate: object, raw_login: str) -> bool:
    return (
        getattr(candidate, "display_name", None) == "Yandex Direct account 123456789"
        and raw_login not in repr(candidate)
    )


def test_non_rfc_provider_page_fails_before_repository_reconciliation() -> None:
    repository = _RecordingRepository()
    attempts = 0
    sleeps: list[float] = []
    raw_marker = "synthetic-non-rfc-service-login"
    body = (
        '{"result":{"Clients":[{"ClientId":123456789,"Login":"'
        + raw_marker
        + '","ClientInfo":"Synthetic non-RFC service account","Type":"CLIENT",'
        '"Archived":"NO","Representatives":Infinity}]}}'
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, content=body)

    provider = HttpxYandexDirectAccountProvider(
        access_token="synthetic-discovery-access-token",
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )

    with pytest.raises(AccountDiscoveryFailure) as raised:
        AccountDiscoveryService(repository=repository).discover(
            user_id=_USER_ID,
            workspace_id=_WORKSPACE_ID,
            connection_id=_CONNECTION_ID,
            provider=provider,
            now=_NOW,
        )

    assert raised.value.kind == "provider_malformed_response"
    assert attempts == 1
    assert sleeps == []
    assert repository.reconcile_calls == []
    assert _service_failure_is_redacted(failure=raised.value, raw_marker=raw_marker) is True


def _service_failure_is_redacted(*, failure: AccountDiscoveryFailure, raw_marker: str) -> bool:
    return raw_marker not in str(failure) and raw_marker not in repr(failure)


def test_agency_holder_with_missing_required_identity_fails_before_reconciliation() -> None:
    repository = _RecordingRepository()

    class _MalformedAgencyProvider:
        def clients_get(self) -> DirectAccountDiscoveryPage:
            return DirectAccountDiscoveryPage(
                client_rows=(
                    {
                        "Login": "synthetic-agency-login",
                        "Type": "AGENCY",
                        "Archived": "NO",
                    },
                )
            )

        def agencyclients_get(self, *, offset: int) -> DirectAccountDiscoveryPage:
            assert offset == 0
            return DirectAccountDiscoveryPage(client_rows=())

    with pytest.raises(AccountDiscoveryFailure) as raised:
        AccountDiscoveryService(repository=repository).discover(
            user_id=_USER_ID,
            workspace_id=_WORKSPACE_ID,
            connection_id=_CONNECTION_ID,
            provider=_MalformedAgencyProvider(),
            now=_NOW,
        )

    assert raised.value.kind == "provider_malformed_response"
    assert repository.reconcile_calls == []
