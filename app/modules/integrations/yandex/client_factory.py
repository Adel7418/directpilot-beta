from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import ProviderAccountRecord, YandexProviderConnectionRecord
from app.db.rls import WorkspaceContextError, tenant_transaction
from app.modules.integrations.yandex.credentials import (
    CredentialContext,
    CredentialVault,
    CredentialVaultError,
    EncryptedProviderAccountLogin,
    EncryptedYandexCredential,
    ProviderAccountLoginContext,
)
from app.modules.integrations.yandex.refresh import (
    DEFAULT_REFRESH_SKEW_SECONDS,
    is_refresh_due,
)
from app.modules.tenancy.authorization import PostgresWorkspaceAuthorizer
from app.modules.tenancy.policy import AuthorizationDenied, Capability
from app.yandex_direct import YandexDirectClient

_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ALLOWED_RUNTIME_MODES = frozenset({"sandbox", "live_readonly", "live_write"})
_ALLOWED_ACCOUNT_TYPES = frozenset({"advertiser", "agency_client"})
_DIRECT_READ_CAPABILITY = "direct.read"


class DirectClientFactoryError(RuntimeError):
    """Base class for safe connection-scoped Direct client failures."""


class DirectClientAuthorizationDenied(DirectClientFactoryError):
    """The current principal cannot read this workspace."""


class DirectClientBindingNotFound(DirectClientFactoryError):
    """The requested connection/account binding is absent or not tenant-owned."""


class DirectClientBindingUnavailable(DirectClientFactoryError):
    """The requested binding is present but cannot safely serve Direct reads."""


class DirectClientCredentialUnavailable(DirectClientFactoryError):
    """The encrypted connection credential or routing login cannot be used."""


class DirectClientRefreshRequired(DirectClientFactoryError):
    """The access credential is due for P4-04 lifecycle refresh."""


class DirectClientLeaseInvalidated(DirectClientFactoryError):
    """A previously resolved tenant binding no longer matches its lease snapshot."""


class DirectClientResolutionUnavailable(DirectClientFactoryError):
    """The local tenant-bound resolution path is temporarily unavailable."""


class DirectClientClosed(DirectClientResolutionUnavailable):
    """The request-bounded Direct client lease has already been closed."""


@dataclass(frozen=True, slots=True)
class DirectClientRequest:
    user_id: UUID
    workspace_id: UUID
    provider_connection_id: UUID
    provider_account_id: UUID
    request_id: str
    runtime_mode: Literal["sandbox", "live_readonly", "live_write"]
    spend_agency_units: Literal[False] = False

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, UUID)
            for value in (
                self.user_id,
                self.workspace_id,
                self.provider_connection_id,
                self.provider_account_id,
            )
        ):
            raise ValueError("Direct client request is invalid")
        if not isinstance(self.request_id, str) or not _REQUEST_ID_PATTERN.fullmatch(self.request_id):
            raise ValueError("Direct client request is invalid")
        if self.runtime_mode not in _ALLOWED_RUNTIME_MODES or self.spend_agency_units is not False:
            raise ValueError("Direct client request is invalid")


class ConnectionScopedDirectClientFactory(Protocol):
    def create(self, *, request: DirectClientRequest) -> DirectClientLease: ...


class DirectClientLease:
    """A closeable request lease that holds no public credential fields."""

    def __init__(
        self,
        *,
        client: YandexDirectClient,
        connection_id: UUID,
        provider_account_id: UUID,
        request_id: str,
    ) -> None:
        self._client: YandexDirectClient | None = client
        self.connection_id = connection_id
        self.provider_account_id = provider_account_id
        self.request_id = request_id

    @property
    def client(self) -> YandexDirectClient:
        client = self._client
        if client is None:
            raise DirectClientClosed("Yandex Direct client lease is closed")
        return client

    def close(self) -> None:
        client = self._client
        if client is None:
            return
        self._client = None
        client.close()

    def __enter__(self) -> YandexDirectClient:
        return self.client

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()


@dataclass(frozen=True, slots=True)
class _BindingSnapshot:
    user_id: UUID
    workspace_id: UUID
    connection_id: UUID
    provider_account_id: UUID
    connection_version: int
    provider_account_version: int


class _DirectClientLeaseVerifier:
    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        workspace_authorizer: PostgresWorkspaceAuthorizer,
        snapshot: _BindingSnapshot,
    ) -> None:
        self._sessions = sessions
        self._workspace_authorizer = workspace_authorizer
        self._snapshot = snapshot

    def __call__(self) -> None:
        snapshot = self._snapshot
        try:
            self._workspace_authorizer.authorize(
                user_id=snapshot.user_id,
                workspace_id=snapshot.workspace_id,
                capability=Capability.READ_WORKSPACE_DATA,
            )
        except AuthorizationDenied:
            raise DirectClientLeaseInvalidated("Yandex Direct client lease is invalidated") from None
        except (SQLAlchemyError, WorkspaceContextError):
            raise DirectClientResolutionUnavailable(
                "Yandex Direct client resolution is unavailable"
            ) from None

        try:
            with tenant_transaction(
                self._sessions,
                workspace_id=snapshot.workspace_id,
                user_id=snapshot.user_id,
            ) as session:
                connection = session.scalar(
                    select(YandexProviderConnectionRecord)
                    .where(
                        YandexProviderConnectionRecord.id == snapshot.connection_id,
                        YandexProviderConnectionRecord.workspace_id == snapshot.workspace_id,
                        YandexProviderConnectionRecord.provider == "yandex",
                    )
                    .with_for_update()
                )
                account = session.scalar(
                    select(ProviderAccountRecord)
                    .where(
                        ProviderAccountRecord.id == snapshot.provider_account_id,
                        ProviderAccountRecord.workspace_id == snapshot.workspace_id,
                        ProviderAccountRecord.connection_id == snapshot.connection_id,
                    )
                    .with_for_update()
                )
                if connection is None or account is None:
                    raise DirectClientLeaseInvalidated("Yandex Direct client lease is invalidated")
                if not _binding_is_available(connection=connection, account=account):
                    raise DirectClientLeaseInvalidated("Yandex Direct client lease is invalidated")
                if (
                    connection.version != snapshot.connection_version
                    or account.version != snapshot.provider_account_version
                ):
                    raise DirectClientLeaseInvalidated("Yandex Direct client lease is invalidated")
        except DirectClientLeaseInvalidated:
            raise
        except (SQLAlchemyError, WorkspaceContextError):
            raise DirectClientResolutionUnavailable(
                "Yandex Direct client resolution is unavailable"
            ) from None


class PostgresConnectionScopedDirectClientFactory:
    """Resolve one authorized active Yandex account into a read-only Direct lease."""

    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        vault: CredentialVault,
        workspace_authorizer: PostgresWorkspaceAuthorizer,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._sessions = sessions
        self._vault = vault
        self._workspace_authorizer = workspace_authorizer
        self._settings = settings
        self._transport = transport

    def create(self, *, request: DirectClientRequest) -> DirectClientLease:
        self._authorize(request=request)
        snapshot, access_token, client_login = self._resolve_binding(request=request)
        verifier = _DirectClientLeaseVerifier(
            sessions=self._sessions,
            workspace_authorizer=self._workspace_authorizer,
            snapshot=snapshot,
        )
        try:
            client = YandexDirectClient(
                settings=self._settings,
                access_token=access_token,
                client_login=client_login,
                use_operator_units=False,
                request_context=verifier,
                transport=self._transport,
            )
        finally:
            del access_token
            del client_login
        return DirectClientLease(
            client=client,
            connection_id=snapshot.connection_id,
            provider_account_id=snapshot.provider_account_id,
            request_id=request.request_id,
        )

    def _authorize(self, *, request: DirectClientRequest) -> None:
        try:
            self._workspace_authorizer.authorize(
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                capability=Capability.READ_WORKSPACE_DATA,
            )
        except AuthorizationDenied:
            raise DirectClientAuthorizationDenied("Yandex Direct client authorization is denied") from None
        except (SQLAlchemyError, WorkspaceContextError):
            raise DirectClientResolutionUnavailable(
                "Yandex Direct client resolution is unavailable"
            ) from None

    def _resolve_binding(
        self,
        *,
        request: DirectClientRequest,
    ) -> tuple[_BindingSnapshot, str, str | None]:
        try:
            with tenant_transaction(
                self._sessions,
                workspace_id=request.workspace_id,
                user_id=request.user_id,
            ) as session:
                connection = session.scalar(
                    select(YandexProviderConnectionRecord)
                    .where(
                        YandexProviderConnectionRecord.id == request.provider_connection_id,
                        YandexProviderConnectionRecord.workspace_id == request.workspace_id,
                        YandexProviderConnectionRecord.provider == "yandex",
                    )
                    .with_for_update()
                )
                if connection is None:
                    raise DirectClientBindingNotFound("Yandex Direct client binding is not found")
                account = session.scalar(
                    select(ProviderAccountRecord)
                    .where(
                        ProviderAccountRecord.id == request.provider_account_id,
                        ProviderAccountRecord.workspace_id == request.workspace_id,
                        ProviderAccountRecord.connection_id == request.provider_connection_id,
                    )
                    .with_for_update()
                )
                if account is None:
                    raise DirectClientBindingNotFound("Yandex Direct client binding is not found")
                if (
                    connection.id != request.provider_connection_id
                    or connection.workspace_id != request.workspace_id
                    or connection.provider != "yandex"
                    or account.id != request.provider_account_id
                    or account.workspace_id != request.workspace_id
                    or account.connection_id != connection.id
                ):
                    raise DirectClientBindingNotFound("Yandex Direct client binding is not found")
                if not _binding_is_available(connection=connection, account=account):
                    raise DirectClientBindingUnavailable(
                        "Yandex Direct client binding is unavailable"
                    )

                try:
                    credential = self._vault.decrypt(
                        context=CredentialContext.for_yandex_connection(
                            workspace_id=request.workspace_id,
                            connection_id=connection.id,
                        ),
                        encrypted=_encrypted_credential(connection),
                    )
                    try:
                        if is_refresh_due(
                            now=_utc_now(),
                            expires_at=credential.access_token_expires_at,
                            skew_seconds=DEFAULT_REFRESH_SKEW_SECONDS,
                        ):
                            raise DirectClientRefreshRequired(
                                "Yandex Direct client refresh is required"
                            )
                        access_token = credential.access_token
                    finally:
                        del credential

                    client_login: str | None = None
                    if account.account_type == "agency_client":
                        client_login = self._vault.decrypt_provider_account_login(
                            context=ProviderAccountLoginContext.for_yandex_provider_account(
                                workspace_id=request.workspace_id,
                                connection_id=connection.id,
                                provider_account_id=account.id,
                            ),
                            encrypted=_encrypted_provider_account_login(account),
                        )
                except DirectClientRefreshRequired:
                    raise
                except (CredentialVaultError, ValueError):
                    raise DirectClientCredentialUnavailable(
                        "Yandex Direct client credential is unavailable"
                    ) from None

                return (
                    _BindingSnapshot(
                        user_id=request.user_id,
                        workspace_id=request.workspace_id,
                        connection_id=connection.id,
                        provider_account_id=account.id,
                        connection_version=connection.version,
                        provider_account_version=account.version,
                    ),
                    access_token,
                    client_login,
                )
        except (
            DirectClientBindingNotFound,
            DirectClientBindingUnavailable,
            DirectClientCredentialUnavailable,
            DirectClientRefreshRequired,
        ):
            raise
        except (SQLAlchemyError, WorkspaceContextError):
            raise DirectClientResolutionUnavailable(
                "Yandex Direct client resolution is unavailable"
            ) from None


def _binding_is_available(
    *,
    connection: YandexProviderConnectionRecord | None,
    account: ProviderAccountRecord | None,
) -> bool:
    return (
        connection is not None
        and account is not None
        and connection.provider == "yandex"
        and account.workspace_id == connection.workspace_id
        and account.connection_id == connection.id
        and connection.status == "active"
        and account.status == "active"
        and account.account_type in _ALLOWED_ACCOUNT_TYPES
        and _DIRECT_READ_CAPABILITY in account.capabilities
    )


def _encrypted_credential(record: YandexProviderConnectionRecord) -> EncryptedYandexCredential:
    return EncryptedYandexCredential(
        token_ciphertext=record.token_ciphertext,
        token_nonce=record.token_nonce,
        wrapped_dek=record.wrapped_dek,
        wrap_nonce=record.wrap_nonce,
        kek_key_id=record.kek_key_id,
        schema_version=record.schema_version,
        access_token_expires_at=record.access_token_expires_at,
        refresh_token_expires_at=record.refresh_token_expires_at,
    )


def _encrypted_provider_account_login(
    record: ProviderAccountRecord,
) -> EncryptedProviderAccountLogin:
    return EncryptedProviderAccountLogin(
        ciphertext=record.login_ciphertext,
        nonce=record.login_nonce,
        wrapped_dek=record.login_wrapped_dek,
        wrap_nonce=record.login_wrap_nonce,
        kek_key_id=record.login_kek_key_id,
        schema_version=record.login_schema_version,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
