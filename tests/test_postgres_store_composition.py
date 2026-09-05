from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

import app.repositories.postgres_store as postgres_store


class RecordingAuditRepository:
    def __init__(self, _sessions: object, *, workspace_id: UUID, user_id: UUID) -> None:
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def append_audit(self, action: str, entity: str, **kwargs: Any) -> str:
        self.calls.append((action, entity, kwargs))
        return "recorded"


def test_postgres_store_legacy_audit_uses_explicit_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(postgres_store, "PostgresAuditRepository", RecordingAuditRepository)
    sessions = cast(sessionmaker[Session], None)
    repository = postgres_store.PostgresLegacyStoreRepository(sessions)
    audit = cast(RecordingAuditRepository, repository._audit)

    assert audit.workspace_id == repository._workspace_id
    assert audit.user_id == repository._user_id

    legacy_append_audit = repository._legacy.append_audit

    assert legacy_append_audit.__self__ is repository._legacy
    assert (
        legacy_append_audit(
            "campaign_draft_created",
            "draft-001",
            actor="operator",
            dry_run=False,
            details={"request_id": "request-001"},
        )
        == "recorded"
    )
    assert audit.calls == [
        (
            "campaign_draft_created",
            "draft-001",
            {
                "actor": "operator",
                "dry_run": False,
                "details": {"request_id": "request-001"},
            },
        )
    ]
