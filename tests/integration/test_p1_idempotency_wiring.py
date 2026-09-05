from __future__ import annotations

from collections.abc import Callable

import pytest

from app.config import Settings
from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.models import YandexControlRequest
from app.modules.actions.idempotency import IdempotencyConflictError
from app.repositories.postgres_store import PostgresLegacyStoreRepository


def test_postgres_p1_control_idempotency_replays_after_restart_and_rejects_conflict(
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
    settings = Settings(_env_file=None, directpilot_mode="mock", yandex_oauth_token=None)
    payload = YandexControlRequest(
        approved=True,
        idempotency_key="persistent-control-key-001",
        dry_run=True,
        reason="rehearsal",
    )

    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        first = PostgresLegacyStoreRepository(app_runtime.sessions).yandex_control(
            "campaign-p1-idempotency",
            "pause",
            payload,
            settings=settings,
            client=None,
        )

        restart_postgres()
        app_runtime.close()
        reloaded_runtime = create_database_runtime(
            DatabaseSettings.from_mapping(
                {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "app_url")}
            )
        )
        try:
            repository = PostgresLegacyStoreRepository(reloaded_runtime.sessions)
            replay = repository.yandex_control(
                "campaign-p1-idempotency",
                "pause",
                payload,
                settings=settings,
                client=None,
            )
            assert replay == first

            with pytest.raises(IdempotencyConflictError, match="idempotency key conflicts"):
                repository.yandex_control(
                    "campaign-p1-idempotency",
                    "pause",
                    payload.model_copy(update={"reason": "different-request"}),
                    settings=settings,
                    client=None,
                )
        finally:
            reloaded_runtime.close()
    finally:
        owner_runtime.close()
        app_runtime.close()
