from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    ExternalIdentityRecord,
    OAuthTransactionRecord,
    YandexProviderConnectionRecord,
)
from app.db.rls import tenant_transaction
from app.modules.integrations.yandex.credentials import (
    CredentialContext,
    CredentialVault,
    EncryptedYandexCredential,
    YandexCredentialPayload,
)
from app.modules.integrations.yandex.oauth import (
    ConsumedOAuthTransaction,
    NewOAuthTransaction,
    hash_oauth_state,
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
