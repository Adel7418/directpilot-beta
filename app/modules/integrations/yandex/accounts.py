from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import UUID

from app.modules.integrations.yandex.account_provider import (
    DirectAccountDiscoveryPage,
    DirectAccountDiscoveryProviderFailure,
)

AccountType = Literal["advertiser", "agency_client"]
AccountStatus = Literal["active", "archived", "stale"]


class AccountDiscoveryFailure(RuntimeError):
    """Safe failure from account discovery; provider data is intentionally omitted."""

    def __init__(self, kind: str, *, direct_code: int | None = None) -> None:
        self.kind = kind
        self.direct_code = direct_code
        super().__init__("Yandex account discovery failed")


@dataclass(frozen=True, slots=True)
class ValidatedProviderAccount:
    """Validated internal snapshot row; routing Login cannot appear in repr output."""

    provider_account_key: str
    routing_login: str = field(repr=False)
    display_name: str
    account_type: AccountType
    status: Literal["active", "archived"]
    country_id: int | None
    currency: str | None
    capabilities: tuple[str, ...] = field(default=("direct.read",), init=False)


@dataclass(frozen=True, slots=True)
class DiscoveredProviderAccount:
    """Safe account result for internal callers; no provider key or Login is exposed."""

    id: UUID
    workspace_id: UUID
    connection_id: UUID
    display_name: str
    account_type: AccountType
    status: AccountStatus
    capabilities: tuple[str, ...]
    country_id: int | None
    currency: str | None
    last_discovered_at: datetime
    last_verified_at: datetime


class AccountDiscoveryProvider(Protocol):
    def clients_get(self) -> DirectAccountDiscoveryPage: ...

    def agencyclients_get(self, *, offset: int) -> DirectAccountDiscoveryPage: ...


class ProviderAccountDiscoveryRepository(Protocol):
    def begin_discovery(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
    ) -> int: ...

    def reconcile_discovery(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        expected_connection_version: int,
        candidates: tuple[ValidatedProviderAccount, ...],
        now: datetime,
    ) -> tuple[DiscoveredProviderAccount, ...]: ...


class AccountDiscoveryService:
    """Fetches a complete account snapshot before entering a reconciliation transaction."""

    def __init__(self, *, repository: ProviderAccountDiscoveryRepository) -> None:
        self._repository = repository

    def discover(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        provider: AccountDiscoveryProvider,
        now: datetime | None = None,
    ) -> tuple[DiscoveredProviderAccount, ...]:
        observed_at = _utc_now(now)
        expected_connection_version = self._repository.begin_discovery(
            user_id=user_id,
            workspace_id=workspace_id,
            connection_id=connection_id,
        )
        try:
            holder_page = provider.clients_get()
            candidates = _discover_snapshot(provider=provider, holder_page=holder_page)
        except DirectAccountDiscoveryProviderFailure as exc:
            raise AccountDiscoveryFailure(exc.kind, direct_code=exc.direct_code) from None
        return self._repository.reconcile_discovery(
            user_id=user_id,
            workspace_id=workspace_id,
            connection_id=connection_id,
            expected_connection_version=expected_connection_version,
            candidates=candidates,
            now=observed_at,
        )


def _discover_snapshot(
    *,
    provider: AccountDiscoveryProvider,
    holder_page: DirectAccountDiscoveryPage,
) -> tuple[ValidatedProviderAccount, ...]:
    if holder_page.limited_by is not None or len(holder_page.client_rows) != 1:
        raise AccountDiscoveryFailure("provider_malformed_response")
    holder = holder_page.client_rows[0]
    holder_type = _required_text(holder, "Type")
    if holder_type == "CLIENT":
        return (_validated_candidate(holder, account_type="advertiser"),)
    if holder_type == "SUBCLIENT":
        return (_validated_candidate(holder, account_type="agency_client"),)
    if holder_type != "AGENCY":
        raise AccountDiscoveryFailure("provider_malformed_response")
    _validated_candidate(holder, account_type="advertiser")
    return _agency_candidates(provider=provider)


def _agency_candidates(
    *,
    provider: AccountDiscoveryProvider,
) -> tuple[ValidatedProviderAccount, ...]:
    offset = 0
    candidates: list[ValidatedProviderAccount] = []
    seen_keys: set[str] = set()
    login_to_key: dict[str, str] = {}
    while True:
        try:
            page = provider.agencyclients_get(offset=offset)
        except DirectAccountDiscoveryProviderFailure as exc:
            raise AccountDiscoveryFailure(exc.kind, direct_code=exc.direct_code) from None
        for row in page.client_rows:
            if _required_text(row, "Type") != "SUBCLIENT":
                raise AccountDiscoveryFailure("provider_malformed_response")
            candidate = _validated_candidate(row, account_type="agency_client")
            if candidate.provider_account_key in seen_keys:
                raise AccountDiscoveryFailure("provider_malformed_response")
            prior_key = login_to_key.setdefault(
                candidate.routing_login,
                candidate.provider_account_key,
            )
            if prior_key != candidate.provider_account_key:
                raise AccountDiscoveryFailure("provider_malformed_response")
            seen_keys.add(candidate.provider_account_key)
            candidates.append(candidate)
        limited_by = page.limited_by
        if limited_by is None:
            return tuple(candidates)
        if (
            not isinstance(limited_by, int)
            or isinstance(limited_by, bool)
            or limited_by <= offset
            or not page.client_rows
        ):
            raise AccountDiscoveryFailure("provider_malformed_response")
        offset = limited_by


def _validated_candidate(
    row: object,
    *,
    account_type: AccountType,
) -> ValidatedProviderAccount:
    if not isinstance(row, dict):
        raise AccountDiscoveryFailure("provider_malformed_response")
    client_id = row.get("ClientId")
    if not isinstance(client_id, int) or isinstance(client_id, bool) or client_id <= 0:
        raise AccountDiscoveryFailure("provider_malformed_response")
    login = _required_text(row, "Login")
    archived = _required_text(row, "Archived")
    if archived == "NO":
        status: Literal["active", "archived"] = "active"
    elif archived == "YES":
        status = "archived"
    else:
        raise AccountDiscoveryFailure("provider_malformed_response")
    provider_account_key = str(client_id)
    display_name = _display_name(row, provider_account_key=provider_account_key)
    return ValidatedProviderAccount(
        provider_account_key=provider_account_key,
        routing_login=login,
        display_name=display_name,
        account_type=account_type,
        status=status,
        country_id=_optional_country_id(row.get("CountryId")),
        currency=_optional_currency(row.get("Currency")),
    )


def _required_text(row: object, field_name: str) -> str:
    if not isinstance(row, dict):
        raise AccountDiscoveryFailure("provider_malformed_response")
    value = row.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise AccountDiscoveryFailure("provider_malformed_response")
    return value


def _display_name(row: dict[str, object], *, provider_account_key: str) -> str:
    value = row.get("ClientInfo")
    if value is None:
        return _fallback_display_name(provider_account_key)
    if not isinstance(value, str):
        raise AccountDiscoveryFailure("provider_malformed_response")
    return value if value.strip() else _fallback_display_name(provider_account_key)


def _fallback_display_name(provider_account_key: str) -> str:
    return f"Yandex Direct account {provider_account_key}"[:255]


def _optional_country_id(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AccountDiscoveryFailure("provider_malformed_response")
    return value


def _optional_currency(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AccountDiscoveryFailure("provider_malformed_response")
    return value


def _utc_now(value: datetime | None) -> datetime:
    now = datetime.now(timezone.utc) if value is None else value
    if now.tzinfo is None:
        raise ValueError("Account discovery time must be timezone-aware")
    return now.astimezone(timezone.utc)
