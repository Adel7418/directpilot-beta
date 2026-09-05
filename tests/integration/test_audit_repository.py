from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.modules.audit.repository import PostgresAuditRepository


def test_app_role_persists_sanitized_append_only_audit_across_restart(
    postgres_service: object,
    restart_postgres: Callable[[], None],
) -> None:
    owner_runtime = create_database_runtime(
        DatabaseSettings.from_mapping(
            {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "owner_url")}
        )
    )
    app_runtime = create_database_runtime(
        DatabaseSettings.from_mapping(
            {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "app_url")}
        )
    )

    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        repository = PostgresAuditRepository(app_runtime.sessions)
        event = repository.append_audit(
            "campaign_draft_created",
            "draft-001",
            actor="operator",
            dry_run=True,
            details={
                "request_id": "request-001",
                "item_count": 3,
                "mode": "live_readonly",
                "authorization": "discard-this",
                "raw_provider_payload": {"untrusted": "discard-this"},
            },
        )

        assert event.details == {
            "request_id": "request-001",
            "item_count": 3,
            "mode": "live_readonly",
        }

        with app_runtime.engine.connect() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(text("UPDATE audit_events SET action = 'changed'"))
            connection.rollback()
            with pytest.raises(DBAPIError):
                connection.execute(text("DELETE FROM audit_events"))
            connection.rollback()

        restart_postgres()
        app_runtime.close()
        reloaded_runtime = create_database_runtime(
            DatabaseSettings.from_mapping(
                {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "app_url")}
            )
        )
        try:
            persisted = PostgresAuditRepository(reloaded_runtime.sessions).audit_events
        finally:
            reloaded_runtime.close()

        assert [item.id for item in persisted] == [event.id]
        assert persisted[0].details == event.details
    finally:
        owner_runtime.close()
        app_runtime.close()
