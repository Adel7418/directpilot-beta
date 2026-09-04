from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.modules.actions.idempotency import (
    IdempotencyConflictError,
    PostgresIdempotencyRepository,
)


def test_idempotency_replays_conflicts_serializes_concurrency_and_survives_restart(
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
        repository = PostgresIdempotencyRepository(app_runtime.sessions)
        first = repository.claim(
            "campaign.pause",
            "idempotency-key-001",
            {"campaign_id": 101, "dry_run": True},
        )
        assert first.is_owner is True
        completed = repository.complete(
            first,
            status="succeeded",
            result={"status": "ok", "token": "discard-this"},
        )
        assert completed.result == {"status": "ok"}

        replay = repository.claim(
            "campaign.pause",
            "idempotency-key-001",
            {"dry_run": True, "campaign_id": 101},
        )
        assert replay.is_owner is False
        assert replay.status == "succeeded"
        assert replay.result == completed.result

        with pytest.raises(IdempotencyConflictError, match="idempotency key conflicts"):
            repository.claim(
                "campaign.pause",
                "idempotency-key-001",
                {"campaign_id": 102, "dry_run": True},
            )

        restart_postgres()
        app_runtime.close()
        reloaded_runtime = create_database_runtime(
            DatabaseSettings.from_mapping(
                {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "app_url")}
            )
        )
        try:
            reloaded = PostgresIdempotencyRepository(reloaded_runtime.sessions).claim(
                "campaign.pause",
                "idempotency-key-001",
                {"campaign_id": 101, "dry_run": True},
            )
            assert reloaded.is_owner is False
            assert reloaded.result == completed.result
        finally:
            reloaded_runtime.close()

        barrier = threading.Barrier(2)

        def concurrent_claim() -> bool:
            barrier.wait()
            claim = PostgresIdempotencyRepository(app_runtime.sessions).claim(
                "campaign.pause",
                "idempotency-key-concurrent",
                {"campaign_id": 101, "dry_run": True},
            )
            return claim.is_owner

        with ThreadPoolExecutor(max_workers=2) as executor:
            owners = list(executor.map(lambda _: concurrent_claim(), range(2)))

        assert owners.count(True) == 1
        assert owners.count(False) == 1
    finally:
        owner_runtime.close()
        app_runtime.close()
