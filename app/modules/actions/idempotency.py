from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import IdempotencyRecord


class IdempotencyError(RuntimeError):
    """Base failure for the durable idempotency protocol."""


class IdempotencyConflictError(IdempotencyError):
    """Raised when one namespace/key is reused with a different request."""


class IdempotencyStateError(IdempotencyError):
    """Raised when a non-owner attempts to finalize a stored claim."""


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    record_id: UUID
    namespace: str
    key_hash: str
    request_hash: str
    status: str
    result: dict[str, Any] | None
    is_owner: bool


_SENSITIVE_RESULT_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "oauth_token",
        "password",
        "raw_provider_payload",
        "secret",
        "token",
    }
)
_FINAL_STATUSES = frozenset({"failed", "succeeded"})


def canonical_request_hash(payload: Mapping[str, Any]) -> str:
    """Hash an order-independent JSON representation without storing the payload."""

    try:
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise IdempotencyError("idempotency request is not canonical JSON") from None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _sanitize_result(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_result(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_RESULT_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_result(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise IdempotencyError("idempotency result is not JSON")


class PostgresIdempotencyRepository:
    """PostgreSQL uniqueness and row-locking implementation for P2 idempotency."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def claim(
        self,
        namespace: str,
        key: str,
        payload: Mapping[str, Any],
    ) -> IdempotencyClaim:
        if not namespace or not key:
            raise IdempotencyError("idempotency namespace and key are required")

        key_hash = _key_hash(key)
        request_hash = canonical_request_hash(payload)
        candidate = IdempotencyRecord(
            id=uuid4(),
            namespace=namespace,
            key_hash=key_hash,
            request_hash=request_hash,
            status="pending",
            result=None,
        )
        with self._sessions() as session:
            with session.begin():
                inserted = session.execute(
                    insert(IdempotencyRecord)
                    .values(
                        id=candidate.id,
                        namespace=candidate.namespace,
                        key_hash=candidate.key_hash,
                        request_hash=candidate.request_hash,
                        status=candidate.status,
                    )
                    .on_conflict_do_nothing(index_elements=["namespace", "key_hash"])
                    .returning(IdempotencyRecord)
                ).scalar_one_or_none()
                if inserted is not None:
                    return self._to_claim(inserted, is_owner=True)

                existing = session.scalar(
                    select(IdempotencyRecord)
                    .where(
                        IdempotencyRecord.namespace == namespace,
                        IdempotencyRecord.key_hash == key_hash,
                    )
                    .with_for_update()
                )
                if existing is None:
                    raise IdempotencyError("idempotency record is unavailable")
                if existing.request_hash != request_hash:
                    raise IdempotencyConflictError("idempotency key conflicts with a prior request")
                return self._to_claim(existing, is_owner=False)

    def complete(
        self,
        claim: IdempotencyClaim,
        *,
        status: str,
        result: Mapping[str, Any],
    ) -> IdempotencyClaim:
        if not claim.is_owner:
            raise IdempotencyStateError("only the original idempotency claimant can finalize")
        if status not in _FINAL_STATUSES:
            raise IdempotencyStateError("idempotency status is invalid")

        safe_result = _sanitize_result(result)
        if not isinstance(safe_result, dict):
            raise IdempotencyError("idempotency result is not an object")

        with self._sessions() as session:
            with session.begin():
                record = session.scalar(
                    select(IdempotencyRecord)
                    .where(IdempotencyRecord.id == claim.record_id)
                    .with_for_update()
                )
                if record is None or record.request_hash != claim.request_hash:
                    raise IdempotencyStateError("idempotency claim is unavailable")
                if record.status != "pending":
                    raise IdempotencyStateError("idempotency claim was already finalized")
                record.status = status
                record.result = safe_result
                record.completed_at = datetime.now(timezone.utc)
                session.flush()
                return self._to_claim(record, is_owner=True)

    @staticmethod
    def _to_claim(record: IdempotencyRecord, *, is_owner: bool) -> IdempotencyClaim:
        return IdempotencyClaim(
            record_id=record.id,
            namespace=record.namespace,
            key_hash=record.key_hash,
            request_hash=record.request_hash,
            status=record.status,
            result=record.result,
            is_owner=is_owner,
        )
