from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    AuditEventRecord,
    ExternalIdentityRecord,
    OAuthTransactionRecord,
    ProviderAccountRecord,
    YandexProviderConnectionRecord,
)
from app.db.rls import tenant_transaction
from app.modules.audit.repository import sanitize_audit_metadata
from app.modules.integrations.yandex.accounts import (
    AccountStatus,
    AccountType,
    DiscoveredProviderAccount,
    ValidatedProviderAccount,
)
from app.modules.integrations.yandex.credentials import (
    CredentialContext,
    CredentialPayloadError,
    CredentialVault,
    CredentialVaultError,
    EncryptedProviderAccountLogin,
    EncryptedYandexCredential,
    ProviderAccountLoginContext,
    YandexCredentialPayload,
)
from app.modules.integrations.yandex.oauth import (
    ConsumedOAuthTransaction,
    NewOAuthTransaction,
    YandexOAuthTokenSet,
    hash_oauth_state,
)
from app.modules.integrations.yandex.provider import YandexOAuthProviderFailure
from app.modules.integrations.yandex.refresh import (
    DisconnectResult,
    RefreshResult,
    YandexConnectionCredentialUnavailable,
    YandexConnectionLifecycleError,
    YandexConnectionNotActive,
    YandexConnectionNotFound,
    YandexConnectionPersistenceFailure,
    YandexConnectionProviderConfigurationFailure,
    YandexConnectionProviderUnavailable,
    YandexConnectionReauthorizationRequired,
    YandexRefreshProvider,
    is_refresh_due,
)


class ExternalIdentityConflict(RuntimeError):
    """Raised when a Yandex subject would rebind a different local user."""


@dataclass(frozen=True, slots=True)
class BoundExternalIdentity:
    user_id: UUID
    issuer: str = field(repr=False)
    subject: str = field(repr=False)


class PostgresOAuthTransactionRepository:
    """Durably stores and atomically spends browser-bound OAuth transactions."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(self, transaction: NewOAuthTransaction) -> None:
        with tenant_transaction(
            self._sessions,
            workspace_id=transaction.workspace_id,
            user_id=transaction.user_id,
        ) as session:
            session.add(
                OAuthTransactionRecord(
                    id=uuid4(),
                    state_hash=transaction.state_hash,
                    code_verifier=transaction.code_verifier,
                    user_id=transaction.user_id,
                    workspace_id=transaction.workspace_id,
                    browser_session_id=transaction.browser_session_id,
                    return_path=transaction.return_path,
                    created_at=transaction.created_at,
                    expires_at=transaction.expires_at,
                    consumed_at=None,
                )
            )
            session.flush()

    def consume(
        self,
        *,
        state: str,
        user_id: UUID,
        workspace_id: UUID,
        browser_session_id: UUID,
        now: datetime | None = None,
    ) -> ConsumedOAuthTransaction | None:
        consumed_at = _utc_now(now)
        with tenant_transaction(
            self._sessions,
            workspace_id=workspace_id,
            user_id=user_id,
        ) as session:
            record = session.scalar(
                select(OAuthTransactionRecord)
                .where(
                    OAuthTransactionRecord.state_hash == hash_oauth_state(state),
                    OAuthTransactionRecord.user_id == user_id,
                    OAuthTransactionRecord.workspace_id == workspace_id,
                    OAuthTransactionRecord.browser_session_id == browser_session_id,
                    OAuthTransactionRecord.consumed_at.is_(None),
                    OAuthTransactionRecord.expires_at > consumed_at,
                )
                .with_for_update()
            )
            if record is None:
                return None
            record.consumed_at = consumed_at
            session.flush()
            return ConsumedOAuthTransaction(
                id=record.id,
                code_verifier=record.code_verifier,
                user_id=record.user_id,
                workspace_id=record.workspace_id,
                browser_session_id=record.browser_session_id,
                return_path=record.return_path,
                expires_at=record.expires_at,
                consumed_at=consumed_at,
            )


class PostgresExternalIdentityRepository:
    """Maps a stable issuer/subject pair to exactly one existing local user."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def bind_yandex_identity(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        issuer: str,
        subject: str,
        profile_login: str | None,
        profile_display_name: str | None,
        now: datetime | None = None,
    ) -> BoundExternalIdentity:
        if not issuer or not subject:
            raise ValueError("external identity is invalid")
        observed_at = _utc_now(now)
        try:
            with tenant_transaction(
                self._sessions,
                workspace_id=workspace_id,
                user_id=user_id,
            ) as session:
                user_identity = session.scalar(
                    select(ExternalIdentityRecord)
                    .where(
                        ExternalIdentityRecord.user_id == user_id,
                        ExternalIdentityRecord.issuer == issuer,
                    )
                    .with_for_update()
                )
                if user_identity is not None and user_identity.subject != subject:
                    raise ExternalIdentityConflict("external identity is already bound")

                subject_identity = session.scalar(
                    select(ExternalIdentityRecord)
                    .where(
                        ExternalIdentityRecord.issuer == issuer,
                        ExternalIdentityRecord.subject == subject,
                    )
                    .with_for_update()
                )
                if subject_identity is not None and subject_identity.user_id != user_id:
                    raise ExternalIdentityConflict("external identity is already bound")

                record = user_identity or subject_identity
                if record is None:
                    record = ExternalIdentityRecord(
                        id=uuid4(),
                        issuer=issuer,
                        subject=subject,
                        user_id=user_id,
                        profile_login=profile_login,
                        profile_display_name=profile_display_name,
                        created_at=observed_at,
                        updated_at=observed_at,
                        last_authenticated_at=observed_at,
                    )
                    session.add(record)
                else:
                    record.profile_login = profile_login
                    record.profile_display_name = profile_display_name
                    record.updated_at = observed_at
                    record.last_authenticated_at = observed_at
                session.flush()
                return BoundExternalIdentity(
                    user_id=record.user_id,
                    issuer=record.issuer,
                    subject=record.subject,
                )
        except IntegrityError:
            raise ExternalIdentityConflict("external identity is already bound") from None


class ProviderConnectionPersistenceError(RuntimeError):
    """Safe failure while persisting or rotating an encrypted provider connection."""


class ProviderAccountDiscoveryUnavailable(RuntimeError):
    """A same-workspace active Yandex connection was unavailable for discovery."""


class ProviderAccountPersistenceError(RuntimeError):
    """Safe failure while atomically reconciling protected provider accounts."""


@dataclass(frozen=True, slots=True)
class YandexProviderConnection:
    """Safe connection metadata; encrypted fields and tokens are intentionally omitted."""

    id: UUID
    workspace_id: UUID
    external_identity_id: UUID
    provider: str
    status: str
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime | None
    credential_updated_at: datetime
    created_at: datetime
    updated_at: datetime
    version: int


class PostgresYandexProviderConnectionRepository:
    """Stores encrypted Yandex OAuth credentials under tenant RLS and row locks."""

    def __init__(self, sessions: sessionmaker[Session], *, vault: CredentialVault) -> None:
        self._sessions = sessions
        self._vault = vault

    def persist_yandex_oauth_tokens(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        issuer: str,
        subject: str,
        payload: YandexCredentialPayload,
        now: datetime | None = None,
    ) -> YandexProviderConnection:
        observed_at = _utc_now(now)
        try:
            with tenant_transaction(
                self._sessions,
                workspace_id=workspace_id,
                user_id=user_id,
            ) as session:
                external_identity = session.scalar(
                    select(ExternalIdentityRecord)
                    .where(
                        ExternalIdentityRecord.user_id == user_id,
                        ExternalIdentityRecord.issuer == issuer,
                        ExternalIdentityRecord.subject == subject,
                    )
                    .with_for_update()
                )
                if external_identity is None:
                    raise ProviderConnectionPersistenceError(
                        "Yandex external identity is unavailable"
                    )
                record = session.scalar(
                    select(YandexProviderConnectionRecord)
                    .where(
                        YandexProviderConnectionRecord.workspace_id == workspace_id,
                        YandexProviderConnectionRecord.provider == "yandex",
                        YandexProviderConnectionRecord.external_identity_id == external_identity.id,
                    )
                    .with_for_update()
                )
                connection_id = record.id if record is not None else uuid4()
                encrypted = self._vault.encrypt(
                    context=CredentialContext.for_yandex_connection(
                        workspace_id=workspace_id,
                        connection_id=connection_id,
                    ),
                    payload=payload,
                )
                if record is None:
                    record = YandexProviderConnectionRecord(
                        id=connection_id,
                        workspace_id=workspace_id,
                        external_identity_id=external_identity.id,
                        provider="yandex",
                        token_ciphertext=encrypted.token_ciphertext,
                        token_nonce=encrypted.token_nonce,
                        wrapped_dek=encrypted.wrapped_dek,
                        wrap_nonce=encrypted.wrap_nonce,
                        kek_key_id=encrypted.kek_key_id,
                        schema_version=encrypted.schema_version,
                        status="active",
                        access_token_expires_at=encrypted.access_token_expires_at,
                        refresh_token_expires_at=encrypted.refresh_token_expires_at,
                        credential_updated_at=observed_at,
                        created_at=observed_at,
                        updated_at=observed_at,
                        version=1,
                    )
                    session.add(record)
                else:
                    _replace_encrypted_credential(record, encrypted)
                    record.status = "active"
                    record.credential_updated_at = observed_at
                    record.updated_at = observed_at
                    record.version += 1
                session.flush()
                return _safe_provider_connection(record)
        except IntegrityError:
            raise ProviderConnectionPersistenceError(
                "Yandex provider connection could not be persisted"
            ) from None

    def refresh_yandex_connection(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        provider: YandexRefreshProvider,
        skew_seconds: int,
        now: datetime | None = None,
    ) -> RefreshResult:
        """Refresh one due active connection while retaining its PostgreSQL row lock."""
        result: RefreshResult | None = None
        failure: YandexConnectionLifecycleError | None = None
        try:
            with tenant_transaction(
                self._sessions,
                workspace_id=workspace_id,
                user_id=user_id,
            ) as session:
                record = session.scalar(
                    select(YandexProviderConnectionRecord)
                    .where(
                        YandexProviderConnectionRecord.id == connection_id,
                        YandexProviderConnectionRecord.workspace_id == workspace_id,
                        YandexProviderConnectionRecord.provider == "yandex",
                    )
                    .with_for_update()
                )
                lock_checked_at = _utc_now(now)
                if record is None:
                    failure = YandexConnectionNotFound()
                elif record.status != "active":
                    failure = YandexConnectionNotActive()
                elif not is_refresh_due(
                    now=lock_checked_at,
                    expires_at=record.access_token_expires_at,
                    skew_seconds=skew_seconds,
                ):
                    result = _refresh_result(record, refreshed=False)
                else:
                    try:
                        decrypted = self._vault.decrypt(
                            context=CredentialContext.for_yandex_connection(
                                workspace_id=workspace_id,
                                connection_id=record.id,
                            ),
                            encrypted=_encrypted_credential(record),
                        )
                    except CredentialVaultError:
                        _append_lifecycle_audit(
                            session,
                            user_id=user_id,
                            workspace_id=workspace_id,
                            connection_id=connection_id,
                            action="yandex_connection_refresh",
                            outcome="credential_unavailable",
                        )
                        failure = YandexConnectionCredentialUnavailable()
                    else:
                        tokens: YandexOAuthTokenSet | None = None
                        try:
                            tokens = provider.refresh_tokens(refresh_token=decrypted.refresh_token)
                        except YandexOAuthProviderFailure as exc:
                            if exc.kind == "invalid_grant":
                                session.delete(record)
                                _append_lifecycle_audit(
                                    session,
                                    user_id=user_id,
                                    workspace_id=workspace_id,
                                    connection_id=connection_id,
                                    action="yandex_connection_refresh",
                                    outcome="refresh_reauth_required",
                                )
                                failure = YandexConnectionReauthorizationRequired()
                            elif exc.kind in {"invalid_client", "unauthorized_client"}:
                                _append_lifecycle_audit(
                                    session,
                                    user_id=user_id,
                                    workspace_id=workspace_id,
                                    connection_id=connection_id,
                                    action="yandex_connection_refresh",
                                    outcome="provider_configuration_error",
                                )
                                failure = YandexConnectionProviderConfigurationFailure()
                            else:
                                _append_lifecycle_audit(
                                    session,
                                    user_id=user_id,
                                    workspace_id=workspace_id,
                                    connection_id=connection_id,
                                    action="yandex_connection_refresh",
                                    outcome="provider_unavailable",
                                )
                                failure = YandexConnectionProviderUnavailable()
                        finally:
                            del decrypted
                        if failure is None and tokens is None:
                            _append_lifecycle_audit(
                                session,
                                user_id=user_id,
                                workspace_id=workspace_id,
                                connection_id=connection_id,
                                action="yandex_connection_refresh",
                                outcome="provider_response_invalid",
                            )
                            failure = YandexConnectionProviderUnavailable()
                        if tokens is not None:
                            try:
                                refreshed_payload = _refreshed_credential_payload(
                                    tokens,
                                    received_at=_utc_now(now),
                                )
                            except (CredentialPayloadError, ValueError):
                                _append_lifecycle_audit(
                                    session,
                                    user_id=user_id,
                                    workspace_id=workspace_id,
                                    connection_id=connection_id,
                                    action="yandex_connection_refresh",
                                    outcome="provider_response_invalid",
                                )
                                failure = YandexConnectionProviderUnavailable()
                            else:
                                try:
                                    encrypted = self._vault.encrypt(
                                        context=CredentialContext.for_yandex_connection(
                                            workspace_id=workspace_id,
                                            connection_id=record.id,
                                        ),
                                        payload=refreshed_payload,
                                    )
                                except CredentialVaultError:
                                    _append_lifecycle_audit(
                                        session,
                                        user_id=user_id,
                                        workspace_id=workspace_id,
                                        connection_id=connection_id,
                                        action="yandex_connection_refresh",
                                        outcome="refresh_persistence_failed",
                                    )
                                    failure = YandexConnectionPersistenceFailure()
                                else:
                                    _replace_encrypted_credential(record, encrypted)
                                    record.status = "active"
                                    record.credential_updated_at = lock_checked_at
                                    record.updated_at = lock_checked_at
                                    record.version += 1
                                    result = _refresh_result(record, refreshed=True)
                                    _append_lifecycle_audit(
                                        session,
                                        user_id=user_id,
                                        workspace_id=workspace_id,
                                        connection_id=connection_id,
                                        action="yandex_connection_refresh",
                                        outcome="refreshed",
                                    )
                                finally:
                                    del refreshed_payload
                            del tokens
                session.flush()
        except SQLAlchemyError:
            raise YandexConnectionPersistenceFailure() from None
        if failure is not None:
            raise failure
        if result is None:
            raise YandexConnectionPersistenceFailure()
        return result

    def disconnect_yandex_connection(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        now: datetime | None = None,
    ) -> DisconnectResult:
        """Hard-delete one local connection without attempting unsupported upstream revoke."""
        del now
        result: DisconnectResult | None = None
        failure: YandexConnectionLifecycleError | None = None
        try:
            with tenant_transaction(
                self._sessions,
                workspace_id=workspace_id,
                user_id=user_id,
            ) as session:
                record = session.scalar(
                    select(YandexProviderConnectionRecord)
                    .where(
                        YandexProviderConnectionRecord.id == connection_id,
                        YandexProviderConnectionRecord.workspace_id == workspace_id,
                        YandexProviderConnectionRecord.provider == "yandex",
                    )
                    .with_for_update()
                )
                if record is not None:
                    session.delete(record)
                    _append_lifecycle_audit(
                        session,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        connection_id=connection_id,
                        action="yandex_connection_disconnect",
                        outcome="disconnect_local",
                    )
                    result = DisconnectResult(connection_id=connection_id)
                elif _has_prior_disconnect_audit(
                    session,
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                ):
                    result = DisconnectResult(connection_id=connection_id)
                else:
                    failure = YandexConnectionNotFound()
                session.flush()
        except SQLAlchemyError:
            raise YandexConnectionPersistenceFailure() from None
        if failure is not None:
            raise failure
        if result is None:
            raise YandexConnectionPersistenceFailure()
        return result

    def rewrap_to_active_key(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
        now: datetime | None = None,
    ) -> YandexProviderConnection:
        observed_at = _utc_now(now)
        with tenant_transaction(
            self._sessions,
            workspace_id=workspace_id,
            user_id=user_id,
        ) as session:
            record = session.scalar(
                select(YandexProviderConnectionRecord)
                .where(
                    YandexProviderConnectionRecord.id == connection_id,
                    YandexProviderConnectionRecord.workspace_id == workspace_id,
                    YandexProviderConnectionRecord.provider == "yandex",
                )
                .with_for_update()
            )
            if record is None:
                raise ProviderConnectionPersistenceError("Yandex provider connection is unavailable")
            rewrapped = self._vault.rewrap_to_active_key(
                context=CredentialContext.for_yandex_connection(
                    workspace_id=workspace_id,
                    connection_id=record.id,
                ),
                encrypted=_encrypted_credential(record),
            )
            record.wrapped_dek = rewrapped.wrapped_dek
            record.wrap_nonce = rewrapped.wrap_nonce
            record.kek_key_id = rewrapped.kek_key_id
            record.schema_version = rewrapped.schema_version
            record.updated_at = observed_at
            record.version += 1
            session.flush()
            return _safe_provider_connection(record)


class PostgresYandexProviderAccountRepository:
    """Reconciles complete, validated Direct account snapshots under tenant RLS."""

    def __init__(self, sessions: sessionmaker[Session], *, vault: CredentialVault) -> None:
        self._sessions = sessions
        self._vault = vault

    def begin_discovery(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        connection_id: UUID,
    ) -> int:
        try:
            with tenant_transaction(
                self._sessions,
                workspace_id=workspace_id,
                user_id=user_id,
            ) as session:
                connection = session.scalar(
                    select(YandexProviderConnectionRecord).where(
                        YandexProviderConnectionRecord.id == connection_id,
                        YandexProviderConnectionRecord.workspace_id == workspace_id,
                        YandexProviderConnectionRecord.provider == "yandex",
                    )
                )
                if connection is None or connection.status != "active":
                    raise ProviderAccountDiscoveryUnavailable(
                        "Yandex provider connection is unavailable"
                    )
                return connection.version
        except ProviderAccountDiscoveryUnavailable:
            raise
        except SQLAlchemyError:
            raise ProviderAccountPersistenceError(
                "Yandex provider accounts could not be loaded"
            ) from None

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
        _validate_account_snapshot(candidates)
        observed_at = _utc_now(now)
        try:
            with tenant_transaction(
                self._sessions,
                workspace_id=workspace_id,
                user_id=user_id,
            ) as session:
                connection = session.scalar(
                    select(YandexProviderConnectionRecord)
                    .where(
                        YandexProviderConnectionRecord.id == connection_id,
                        YandexProviderConnectionRecord.workspace_id == workspace_id,
                        YandexProviderConnectionRecord.provider == "yandex",
                    )
                    .with_for_update()
                )
                if (
                    connection is None
                    or connection.status != "active"
                    or connection.version != expected_connection_version
                ):
                    raise ProviderAccountDiscoveryUnavailable(
                        "Yandex provider connection is unavailable"
                    )
                existing_records = list(
                    session.scalars(
                        select(ProviderAccountRecord)
                        .where(
                            ProviderAccountRecord.workspace_id == workspace_id,
                            ProviderAccountRecord.connection_id == connection_id,
                        )
                        .with_for_update()
                    )
                )
                existing_by_key = {
                    record.provider_account_key: record for record in existing_records
                }
                if len(existing_by_key) != len(existing_records):
                    raise ProviderAccountPersistenceError(
                        "Yandex provider accounts could not be reconciled"
                    )
                reconciled_by_key: dict[str, ProviderAccountRecord] = {}
                for candidate in candidates:
                    record = existing_by_key.get(candidate.provider_account_key)
                    is_existing = record is not None
                    if record is None:
                        record = ProviderAccountRecord(
                            id=uuid4(),
                            workspace_id=workspace_id,
                            connection_id=connection_id,
                            provider_account_key=candidate.provider_account_key,
                            account_type=candidate.account_type,
                            display_name=candidate.display_name,
                            status=candidate.status,
                            capabilities=["direct.read"],
                            country_id=candidate.country_id,
                            currency=candidate.currency,
                            login_ciphertext=b"pending",
                            login_nonce=b"0" * 12,
                            login_wrapped_dek=b"pending",
                            login_wrap_nonce=b"0" * 12,
                            login_kek_key_id="pending",
                            login_schema_version=1,
                            last_discovered_at=observed_at,
                            last_verified_at=observed_at,
                            created_at=observed_at,
                            updated_at=observed_at,
                            version=1,
                        )
                        session.add(record)
                    encrypted = self._vault.encrypt_provider_account_login(
                        context=ProviderAccountLoginContext.for_yandex_provider_account(
                            workspace_id=workspace_id,
                            connection_id=connection_id,
                            provider_account_id=record.id,
                        ),
                        login=candidate.routing_login,
                    )
                    _replace_protected_provider_account_login(record, encrypted)
                    record.account_type = candidate.account_type
                    record.display_name = candidate.display_name
                    record.status = candidate.status
                    record.capabilities = ["direct.read"]
                    record.country_id = candidate.country_id
                    record.currency = candidate.currency
                    record.last_discovered_at = observed_at
                    record.last_verified_at = observed_at
                    record.updated_at = observed_at
                    if is_existing:
                        record.version += 1
                    reconciled_by_key[candidate.provider_account_key] = record
                returned_keys = set(reconciled_by_key)
                for record in existing_records:
                    if record.provider_account_key not in returned_keys and record.status == "active":
                        record.status = "stale"
                        record.updated_at = observed_at
                        record.version += 1
                connection.updated_at = observed_at
                connection.version += 1
                session.flush()
                return tuple(
                    _safe_provider_account(reconciled_by_key[candidate.provider_account_key])
                    for candidate in candidates
                )
        except (ProviderAccountDiscoveryUnavailable, ProviderAccountPersistenceError):
            raise
        except CredentialVaultError:
            raise ProviderAccountPersistenceError(
                "Yandex provider accounts could not be reconciled"
            ) from None
        except (IntegrityError, SQLAlchemyError):
            raise ProviderAccountPersistenceError(
                "Yandex provider accounts could not be reconciled"
            ) from None


def _validate_account_snapshot(candidates: tuple[ValidatedProviderAccount, ...]) -> None:
    keys: set[str] = set()
    for candidate in candidates:
        if (
            not candidate.provider_account_key
            or candidate.provider_account_key in keys
            or candidate.capabilities != ("direct.read",)
            or candidate.account_type not in {"advertiser", "agency_client"}
            or candidate.status not in {"active", "archived"}
        ):
            raise ProviderAccountPersistenceError(
                "Yandex provider account snapshot is invalid"
            )
        keys.add(candidate.provider_account_key)


def _replace_protected_provider_account_login(
    record: ProviderAccountRecord,
    encrypted: EncryptedProviderAccountLogin,
) -> None:
    record.login_ciphertext = encrypted.ciphertext
    record.login_nonce = encrypted.nonce
    record.login_wrapped_dek = encrypted.wrapped_dek
    record.login_wrap_nonce = encrypted.wrap_nonce
    record.login_kek_key_id = encrypted.kek_key_id
    record.login_schema_version = encrypted.schema_version


def _safe_provider_account(record: ProviderAccountRecord) -> DiscoveredProviderAccount:
    return DiscoveredProviderAccount(
        id=record.id,
        workspace_id=record.workspace_id,
        connection_id=record.connection_id,
        display_name=record.display_name,
        account_type=cast(AccountType, record.account_type),
        status=cast(AccountStatus, record.status),
        capabilities=tuple(record.capabilities),
        country_id=record.country_id,
        currency=record.currency,
        last_discovered_at=record.last_discovered_at,
        last_verified_at=record.last_verified_at,
    )


def _refresh_result(
    record: YandexProviderConnectionRecord,
    *,
    refreshed: bool,
) -> RefreshResult:
    return RefreshResult(
        connection_id=record.id,
        status=record.status,
        refreshed=refreshed,
        access_token_expires_at=record.access_token_expires_at,
        credential_version=record.version,
    )


def _refreshed_credential_payload(
    tokens: YandexOAuthTokenSet,
    *,
    received_at: datetime,
) -> YandexCredentialPayload:
    if (
        not isinstance(tokens.token_type, str)
        or tokens.token_type.lower() != "bearer"
        or not isinstance(tokens.access_token, str)
        or not tokens.access_token
        or not isinstance(tokens.refresh_token, str)
        or not tokens.refresh_token
        or not isinstance(tokens.expires_in, int)
        or isinstance(tokens.expires_in, bool)
        or tokens.expires_in <= 0
    ):
        raise CredentialPayloadError("Yandex refresh response is invalid")
    expires_at = _utc_now(received_at) + timedelta(seconds=tokens.expires_in)
    return YandexCredentialPayload(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_token_expires_at=expires_at,
        refresh_token_expires_at=expires_at,
    )


def _append_lifecycle_audit(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    connection_id: UUID,
    action: str,
    outcome: str,
) -> None:
    session.add(
        AuditEventRecord(
            id=uuid4(),
            workspace_id=workspace_id,
            actor=str(user_id),
            action=action,
            entity=f"yandex_connection:{connection_id}",
            dry_run=False,
            details=sanitize_audit_metadata({"provider": "yandex", "outcome": outcome}),
        )
    )


def _has_prior_disconnect_audit(
    session: Session,
    *,
    workspace_id: UUID,
    connection_id: UUID,
) -> bool:
    audit_id = session.scalar(
        select(AuditEventRecord.id)
        .where(
            AuditEventRecord.workspace_id == workspace_id,
            AuditEventRecord.action == "yandex_connection_disconnect",
            AuditEventRecord.entity == f"yandex_connection:{connection_id}",
        )
        .limit(1)
    )
    return audit_id is not None


def _replace_encrypted_credential(
    record: YandexProviderConnectionRecord,
    encrypted: EncryptedYandexCredential,
) -> None:
    record.token_ciphertext = encrypted.token_ciphertext
    record.token_nonce = encrypted.token_nonce
    record.wrapped_dek = encrypted.wrapped_dek
    record.wrap_nonce = encrypted.wrap_nonce
    record.kek_key_id = encrypted.kek_key_id
    record.schema_version = encrypted.schema_version
    record.access_token_expires_at = encrypted.access_token_expires_at
    record.refresh_token_expires_at = encrypted.refresh_token_expires_at


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


def _safe_provider_connection(record: YandexProviderConnectionRecord) -> YandexProviderConnection:
    return YandexProviderConnection(
        id=record.id,
        workspace_id=record.workspace_id,
        external_identity_id=record.external_identity_id,
        provider=record.provider,
        status=record.status,
        access_token_expires_at=record.access_token_expires_at,
        refresh_token_expires_at=record.refresh_token_expires_at,
        credential_updated_at=record.credential_updated_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
        version=record.version,
    )


def _utc_now(value: datetime | None) -> datetime:
    now = datetime.now(timezone.utc) if value is None else value
    if now.tzinfo is None:
        raise ValueError("external identity time must be timezone-aware")
    return now.astimezone(timezone.utc)
