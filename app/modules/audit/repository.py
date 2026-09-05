from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AuditEventRecord
from app.db.rls import (
    LEGACY_OPERATOR_USER_ID,
    LEGACY_OPERATOR_WORKSPACE_ID,
    tenant_transaction,
)
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
    """Append-only PostgreSQL audit records scoped to one explicit workspace."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        workspace_id: UUID | None = LEGACY_OPERATOR_WORKSPACE_ID,
        user_id: UUID = LEGACY_OPERATOR_USER_ID,
    ) -> None:
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._user_id = user_id

    def append_audit(
        self,
        action: str,
        entity: str,
        *,
        actor: str = "agent",
        dry_run: bool = True,
        details: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        if self._workspace_id is None and not action.startswith("platform_auth."):
            raise ValueError("audit workspace is required")
        record = AuditEventRecord(
            id=uuid4(),
            workspace_id=self._workspace_id,
            actor=actor,
            action=action,
            entity=entity,
            dry_run=dry_run,
            details=sanitize_audit_metadata(details) or None,
        )
        if self._workspace_id is None:
            with self._sessions() as session:
                with session.begin():
                    session.add(record)
        else:
            with tenant_transaction(
                self._sessions,
                workspace_id=self._workspace_id,
                user_id=self._user_id,
            ) as session:
                session.add(record)

        return self._to_model(record)

    @property
    def audit_events(self) -> list[AuditEvent]:
        if self._workspace_id is None:
            return []
        with tenant_transaction(
            self._sessions,
            workspace_id=self._workspace_id,
            user_id=self._user_id,
        ) as session:
            records = session.scalars(
                select(AuditEventRecord)
                .where(AuditEventRecord.workspace_id == self._workspace_id)
                .order_by(
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
