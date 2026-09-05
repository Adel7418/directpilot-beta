from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ExternalIdentityRecord, OAuthTransactionRecord
from app.db.rls import tenant_transaction
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


def _utc_now(value: datetime | None) -> datetime:
    now = datetime.now(timezone.utc) if value is None else value
    if now.tzinfo is None:
        raise ValueError("external identity time must be timezone-aware")
    return now.astimezone(timezone.utc)
