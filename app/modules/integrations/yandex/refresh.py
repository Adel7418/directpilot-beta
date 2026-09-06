from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from app.modules.integrations.yandex.oauth import YandexOAuthTokenSet

DEFAULT_REFRESH_SKEW_SECONDS = 60


class YandexConnectionLifecycleError(RuntimeError):
    """Base class for token-free Yandex connection lifecycle outcomes."""

    def __init__(self) -> None:
        super().__init__("Yandex connection lifecycle operation failed")


class YandexConnectionNotFound(YandexConnectionLifecycleError):
    """Raised when a workspace-scoped connection is absent or inaccessible."""


class YandexConnectionNotActive(YandexConnectionLifecycleError):
    """Raised when a retained connection cannot safely refresh."""


class YandexConnectionCredentialUnavailable(YandexConnectionLifecycleError):
    """Raised when an encrypted credential cannot be used safely."""


class YandexConnectionReauthorizationRequired(YandexConnectionLifecycleError):
    """Raised after invalid_grant destroys the local credential material."""


class YandexConnectionProviderUnavailable(YandexConnectionLifecycleError):
    """Raised for safe upstream/transport/response failures."""


class YandexConnectionProviderConfigurationFailure(YandexConnectionLifecycleError):
    """Raised for safe upstream client/application configuration failures."""


class YandexConnectionPersistenceFailure(YandexConnectionLifecycleError):
    """Raised when a provider result cannot be safely committed."""


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Safe metadata returned after an explicit connection refresh attempt."""

    connection_id: UUID
    status: str
    refreshed: bool
    access_token_expires_at: datetime
    credential_version: int


@dataclass(frozen=True, slots=True)
class DisconnectResult:
    """Safe metadata returned after local credential deletion."""

    connection_id: UUID
    status: str = "disconnected"
    local_credentials_purged: bool = True
    provider_revocation: str = "not_supported_for_current_grant"
    yandex_revocation_url: str = "https://id.yandex.ru/personal/data-access"


class YandexRefreshProvider(Protocol):
    def refresh_tokens(self, *, refresh_token: str) -> YandexOAuthTokenSet: ...


class YandexConnectionLifecycleRepository(Protocol):
    def refresh_yandex_connection(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        provider: YandexRefreshProvider,
        skew_seconds: int,
        now: datetime | None = None,
    ) -> RefreshResult: ...

    def disconnect_yandex_connection(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        now: datetime | None = None,
    ) -> DisconnectResult: ...


class YandexConnectionLifecycle(Protocol):
    def refresh(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        now: datetime | None = None,
    ) -> RefreshResult: ...

    def disconnect(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        now: datetime | None = None,
    ) -> DisconnectResult: ...


class YandexConnectionLifecycleService:
    """Explicit P4-04 refresh/disconnect boundary; no transparent client refresh."""

    def __init__(
        self,
        *,
        repository: YandexConnectionLifecycleRepository,
        provider: YandexRefreshProvider,
        refresh_skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
    ) -> None:
        _validated_skew_seconds(refresh_skew_seconds)
        self._repository = repository
        self._provider = provider
        self._refresh_skew_seconds = refresh_skew_seconds

    @property
    def repository(self) -> YandexConnectionLifecycleRepository:
        return self._repository

    @property
    def provider(self) -> YandexRefreshProvider:
        return self._provider

    def refresh(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        now: datetime | None = None,
    ) -> RefreshResult:
        return self._repository.refresh_yandex_connection(
            user_id=user_id,
            workspace_id=workspace_id,
            connection_id=connection_id,
            provider=self._provider,
            skew_seconds=self._refresh_skew_seconds,
            now=now,
        )

    def disconnect(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        now: datetime | None = None,
    ) -> DisconnectResult:
        return self._repository.disconnect_yandex_connection(
            user_id=user_id,
            workspace_id=workspace_id,
            connection_id=connection_id,
            now=now,
        )


def is_refresh_due(*, now: datetime, expires_at: datetime, skew_seconds: int) -> bool:
    """Return true at the exact expiry-minus-skew boundary, always in UTC."""
    _validated_skew_seconds(skew_seconds)
    return _utc_timestamp(now) >= _utc_timestamp(expires_at) - timedelta(seconds=skew_seconds)


def _validated_skew_seconds(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("Yandex refresh skew must be a non-negative integer")


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Yandex refresh time must be timezone-aware")
    return value.astimezone(timezone.utc)
