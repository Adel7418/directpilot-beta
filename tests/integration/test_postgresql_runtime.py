from __future__ import annotations

from app.db.engine import (
    DatabaseSettings,
    check_database_connection,
    create_database_runtime,
)


def test_sync_runtime_connects_to_isolated_postgresql(postgres_service: object) -> None:
    runtime = create_database_runtime(
        DatabaseSettings.from_mapping(
            {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "owner_url")}
        )
    )

    try:
        assert check_database_connection(runtime) >= 170000
    finally:
        runtime.close()
