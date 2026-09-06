from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from app.modules.integrations.yandex.account_provider import DirectAccountDiscoveryPage
from app.modules.integrations.yandex.accounts import (
    AccountDiscoveryFailure,
    AccountDiscoveryService,
)

_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000021")
_CONNECTION_ID = UUID("00000000-0000-4000-8000-000000000022")
_USER_ID = UUID("00000000-0000-4000-8000-000000000023")
_NOW = datetime(2026, 9, 6, 14, tzinfo=timezone.utc)


class _Repository:
    def __init__(self) -> None:
        self.reconciled: list[tuple[tuple[Any, ...], int]] = []

    def begin_discovery(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
    ) -> int:
        assert (user_id, workspace_id, connection_id) == (
            _USER_ID,
            _WORKSPACE_ID,
            _CONNECTION_ID,
        )
        return 11

    def reconcile_discovery(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        expected_connection_version: int,
        candidates: tuple[Any, ...],
        now: datetime,
    ) -> tuple[Any, ...]:
        assert (user_id, workspace_id, connection_id, now) == (
            _USER_ID,
            _WORKSPACE_ID,
            _CONNECTION_ID,
            _NOW,
        )
        self.reconciled.append((candidates, expected_connection_version))
        return ()


class _ScriptedProvider:
    def __init__(
        self,
        *,
        holder: dict[str, object],
        agency_pages: tuple[DirectAccountDiscoveryPage, ...] = (),
    ) -> None:
        self._holder = holder
        self._agency_pages = list(agency_pages)
        self.offsets: list[int] = []

    def clients_get(self) -> DirectAccountDiscoveryPage:
        return DirectAccountDiscoveryPage(client_rows=(self._holder,))

    def agencyclients_get(self, *, offset: int) -> DirectAccountDiscoveryPage:
        self.offsets.append(offset)
        if not self._agency_pages:
            raise AssertionError("unexpected agency page request")
        return self._agency_pages.pop(0)


def _row(
    *,
    client_id: int,
    login: str,
    account_type: str,
    archived: str = "NO",
) -> dict[str, object]:
    return {
        "ClientId": client_id,
        "Login": login,
        "ClientInfo": f"Synthetic {client_id}",
        "Type": account_type,
        "Archived": archived,
        "CountryId": 225,
        "Currency": "RUB",
        "Grants": [{"Privilege": "EDIT_CAMPAIGNS", "Value": "YES"}],
        "Representatives": [{"Role": "CHIEF"}],
    }


def _discover(
    *,
    repository: _Repository,
    provider: _ScriptedProvider,
) -> tuple[Any, ...]:
    return AccountDiscoveryService(repository=repository).discover(
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        connection_id=_CONNECTION_ID,
        provider=provider,
        now=_NOW,
    )


def test_subclient_holder_becomes_one_ordinary_agency_client_account() -> None:
    repository = _Repository()

    result = _discover(
        repository=repository,
        provider=_ScriptedProvider(
            holder=_row(
                client_id=101,
                login="synthetic-subclient-holder",
                account_type="SUBCLIENT",
            )
        ),
    )

    assert result == ()
    candidates, version = repository.reconciled[0]
    assert version == 11
    assert len(candidates) == 1
    assert candidates[0].account_type == "agency_client"
    assert candidates[0].provider_account_key == "101"
    assert candidates[0].capabilities == ("direct.read",)


def test_agency_uses_limited_by_for_next_offset_and_persists_no_container() -> None:
    repository = _Repository()
    provider = _ScriptedProvider(
        holder=_row(
            client_id=201,
            login="synthetic-agency-holder",
            account_type="AGENCY",
        ),
        agency_pages=(
            DirectAccountDiscoveryPage(
                client_rows=(
                    _row(
                        client_id=301,
                        login="synthetic-agency-client-one",
                        account_type="SUBCLIENT",
                    ),
                ),
                limited_by=10_000,
            ),
            DirectAccountDiscoveryPage(
                client_rows=(
                    _row(
                        client_id=302,
                        login="synthetic-agency-client-two",
                        account_type="SUBCLIENT",
                    ),
                )
            ),
        ),
    )

    _discover(repository=repository, provider=provider)

    assert provider.offsets == [0, 10_000]
    candidates, _ = repository.reconciled[0]
    assert [candidate.provider_account_key for candidate in candidates] == ["301", "302"]
    assert {candidate.account_type for candidate in candidates} == {"agency_client"}
    assert "201" not in {candidate.provider_account_key for candidate in candidates}
    assert all(candidate.capabilities == ("direct.read",) for candidate in candidates)


def test_empty_agency_client_list_is_a_complete_successful_snapshot() -> None:
    repository = _Repository()
    provider = _ScriptedProvider(
        holder=_row(
            client_id=401,
            login="synthetic-empty-agency-holder",
            account_type="AGENCY",
        ),
        agency_pages=(DirectAccountDiscoveryPage(client_rows=()),),
    )

    assert _discover(repository=repository, provider=provider) == ()
    assert provider.offsets == [0]
    assert repository.reconciled == [((), 11)]


@pytest.mark.parametrize(
    "pages",
    [
        (
            DirectAccountDiscoveryPage(
                client_rows=(
                    _row(
                        client_id=501,
                        login="synthetic-invalid-type",
                        account_type="CLIENT",
                    ),
                )
            ),
        ),
        (
            DirectAccountDiscoveryPage(
                client_rows=(
                    _row(
                        client_id=502,
                        login="synthetic-duplicate-key",
                        account_type="SUBCLIENT",
                    ),
                    _row(
                        client_id=502,
                        login="synthetic-duplicate-key",
                        account_type="SUBCLIENT",
                    ),
                )
            ),
        ),
        (
            DirectAccountDiscoveryPage(
                client_rows=(
                    _row(
                        client_id=503,
                        login="synthetic-conflicting-login",
                        account_type="SUBCLIENT",
                    ),
                    _row(
                        client_id=504,
                        login="synthetic-conflicting-login",
                        account_type="SUBCLIENT",
                    ),
                )
            ),
        ),
        (DirectAccountDiscoveryPage(client_rows=(), limited_by=10_000),),
        (
            DirectAccountDiscoveryPage(
                client_rows=(
                    _row(
                        client_id=505,
                        login="synthetic-invalid-pagination",
                        account_type="SUBCLIENT",
                    ),
                ),
                limited_by=0,
            ),
        ),
    ],
)
def test_invalid_agency_rows_or_pagination_fail_closed_without_reconciliation(
    pages: tuple[DirectAccountDiscoveryPage, ...],
) -> None:
    repository = _Repository()
    provider = _ScriptedProvider(
        holder=_row(
            client_id=500,
            login="synthetic-invalid-agency-holder",
            account_type="AGENCY",
        ),
        agency_pages=pages,
    )

    with pytest.raises(AccountDiscoveryFailure) as raised:
        _discover(repository=repository, provider=provider)

    assert raised.value.kind == "provider_malformed_response"
    assert repository.reconciled == []
