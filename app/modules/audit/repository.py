from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AuditEventRecord
from app.models import AuditEvent

_SAFE_METADATA_KEYS = frozenset(
    {
        "item_count",
        "mode",
        "operation",
        "outcome",
        "provider",
        "reason_code",
        "request_id",
    }
)
_SAFE_METADATA_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def sanitize_audit_metadata(details: Mapping[str, Any] | None) -> dict[str, str | int | bool]:
    """Keep only bounded, non-nested metadata that belongs in an audit record."""

    if details is None:
        return {}

    sanitized: dict[str, str | int | bool] = {}
    for key, value in details.items():
        if key not in _SAFE_METADATA_KEYS:
            continue
        if isinstance(value, bool):
            sanitized[key] = value
        elif isinstance(value, int) and -1_000_000 <= value <= 1_000_000:
            sanitized[key] = value
        elif isinstance(value, str) and _SAFE_METADATA_VALUE.fullmatch(value):
            sanitized[key] = value
    return sanitized


class PostgresAuditRepository:
    """Append-only PostgreSQL implementation of the P1 audit seam."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def append_audit(
        self,
        action: str,
        entity: str,
        *,
        actor: str = "agent",
        dry_run: bool = True,
        details: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        record = AuditEventRecord(
            id=uuid4(),
            actor=actor,
            action=action,
            entity=entity,
            dry_run=dry_run,
            details=sanitize_audit_metadata(details) or None,
        )
        with self._sessions() as session:
            with session.begin():
                session.add(record)

        return self._to_model(record)

    @property
    def audit_events(self) -> list[AuditEvent]:
        with self._sessions() as session:
            records = session.scalars(
                select(AuditEventRecord).order_by(
                    AuditEventRecord.occurred_at,
                    AuditEventRecord.id,
                )
            ).all()
        return [self._to_model(record) for record in records]

    @staticmethod
    def _to_model(record: AuditEventRecord) -> AuditEvent:
        return AuditEvent(
            id=str(record.id),
            actor=record.actor,
            action=record.action,
            entity=record.entity,
            dry_run=record.dry_run,
            details=record.details,
        )
