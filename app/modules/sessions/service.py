from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import SessionRecord


@dataclass(frozen=True, slots=True)
class SessionLifetime:
    idle: timedelta = timedelta(hours=8)
    absolute: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        if self.idle <= timedelta() or self.absolute <= timedelta():
            raise ValueError("session lifetimes must be positive")


@dataclass(frozen=True, slots=True)
class IssuedSession:
    id: UUID
    user_id: UUID
    active_workspace_id: UUID | None
    expires_at: datetime
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    id: UUID
    user_id: UUID
    active_workspace_id: UUID | None
    expires_at: datetime


def hash_session_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now(value: datetime | None) -> datetime:
    now = datetime.now(timezone.utc) if value is None else value
    if now.tzinfo is None:
        raise ValueError("session time must be timezone-aware")
    return now.astimezone(timezone.utc)


class PostgresSessionService:
    """Opaque browser sessions: only irreversible token hashes reach PostgreSQL."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        lifetime: SessionLifetime | None = None,
    ) -> None:
        self._sessions = sessions
        self._lifetime = lifetime or SessionLifetime()

    def issue(
        self,
        *,
        user_id: UUID,
        active_workspace_id: UUID | None = None,
        now: datetime | None = None,
    ) -> IssuedSession:
        issued_at = _utc_now(now)
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = issued_at + self._lifetime.absolute
        record = SessionRecord(
            id=uuid4(),
            token_hash=hash_session_secret(token),
            csrf_secret_hash=hash_session_secret(csrf_token),
            user_id=user_id,
            active_workspace_id=active_workspace_id,
            created_at=issued_at,
            last_seen_at=issued_at,
            idle_expires_at=min(issued_at + self._lifetime.idle, expires_at),
            expires_at=expires_at,
            revoked_at=None,
        )
        with self._sessions() as session:
            with session.begin():
                session.add(record)
                session.flush()
        return self._to_issued(record, token=token, csrf_token=csrf_token)

    def authenticate(
        self,
        token: str,
        *,
        now: datetime | None = None,
        touch: bool = True,
    ) -> AuthenticatedSession | None:
        observed_at = _utc_now(now)
        token_hash = hash_session_secret(token)
        with self._sessions() as session:
            with session.begin():
                record = session.scalar(
                    select(SessionRecord).where(SessionRecord.token_hash == token_hash)
                )
                if record is None or not self._is_active(record, observed_at):
                    if record is not None and record.revoked_at is None:
                        record.revoked_at = observed_at
                    return None
                if touch:
                    record.last_seen_at = observed_at
                    record.idle_expires_at = min(
                        observed_at + self._lifetime.idle,
                        record.expires_at,
                    )
                return self._to_authenticated(record)

    def verify_csrf(
        self,
        token: str,
        csrf_token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        principal = self.authenticate(token, now=now)
        if principal is None:
            return False
        csrf_hash = hash_session_secret(csrf_token)
        with self._sessions() as session:
            record = session.scalar(select(SessionRecord).where(SessionRecord.id == principal.id))
        return record is not None and hmac.compare_digest(record.csrf_secret_hash, csrf_hash)

    def rotate(self, token: str, *, now: datetime | None = None) -> IssuedSession | None:
        rotated_at = _utc_now(now)
        token_hash = hash_session_secret(token)
        next_token = secrets.token_urlsafe(32)
        next_csrf_token = secrets.token_urlsafe(32)
        with self._sessions() as session:
            with session.begin():
                current = session.scalar(
                    select(SessionRecord)
                    .where(SessionRecord.token_hash == token_hash)
                    .with_for_update()
                )
                if current is None or not self._is_active(current, rotated_at):
                    if current is not None and current.revoked_at is None:
                        current.revoked_at = rotated_at
                    return None
                current.revoked_at = rotated_at
                replacement = SessionRecord(
                    id=uuid4(),
                    token_hash=hash_session_secret(next_token),
                    csrf_secret_hash=hash_session_secret(next_csrf_token),
                    user_id=current.user_id,
                    active_workspace_id=current.active_workspace_id,
                    created_at=rotated_at,
                    last_seen_at=rotated_at,
                    idle_expires_at=min(
                        rotated_at + self._lifetime.idle,
                        current.expires_at,
                    ),
                    expires_at=current.expires_at,
                    revoked_at=None,
                )
                session.add(replacement)
                session.flush()
        return self._to_issued(replacement, token=next_token, csrf_token=next_csrf_token)

    def revoke(self, token: str, *, now: datetime | None = None) -> bool:
        revoked_at = _utc_now(now)
        token_hash = hash_session_secret(token)
        with self._sessions() as session:
            with session.begin():
                record = session.scalar(
                    select(SessionRecord)
                    .where(SessionRecord.token_hash == token_hash)
                    .with_for_update()
                )
                if record is None or record.revoked_at is not None:
                    return False
                record.revoked_at = revoked_at
                return True

    def logout(self, token: str, *, now: datetime | None = None) -> bool:
        return self.revoke(token, now=now)

    @staticmethod
    def _is_active(record: SessionRecord, observed_at: datetime) -> bool:
        return (
            record.revoked_at is None
            and observed_at < record.idle_expires_at
            and observed_at < record.expires_at
        )

    @staticmethod
    def _to_authenticated(record: SessionRecord) -> AuthenticatedSession:
        return AuthenticatedSession(
            id=record.id,
            user_id=record.user_id,
            active_workspace_id=record.active_workspace_id,
            expires_at=record.expires_at,
        )

    @staticmethod
    def _to_issued(
        record: SessionRecord,
        *,
        token: str,
        csrf_token: str,
    ) -> IssuedSession:
        return IssuedSession(
            id=record.id,
            user_id=record.user_id,
            active_workspace_id=record.active_workspace_id,
            expires_at=record.expires_at,
            token=token,
            csrf_token=csrf_token,
        )
