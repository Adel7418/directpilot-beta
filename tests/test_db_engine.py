from __future__ import annotations

import pytest

from app.db.engine import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    DatabaseSettings,
    check_database_connection,
    create_database_runtime,
)


def test_database_settings_fail_closed_when_url_is_absent() -> None:
    with pytest.raises(DatabaseConfigurationError, match="database configuration is required"):
        DatabaseSettings.from_mapping({})


def test_database_settings_accepts_only_psycopg_postgresql_urls() -> None:
    settings = DatabaseSettings.from_mapping(
        {"DIRECTPILOT_DATABASE_URL": "postgresql+psycopg://app:synthetic@127.0.0.1:5432/directpilot"}
    )

    assert settings.url.drivername == "postgresql+psycopg"
    assert settings.connect_timeout_seconds == 5

    with pytest.raises(DatabaseConfigurationError, match="psycopg"):
        DatabaseSettings.from_mapping({"DIRECTPILOT_DATABASE_URL": "sqlite:///unsafe.db"})


def test_database_connection_check_fails_closed_without_a_reachable_server() -> None:
    runtime = create_database_runtime(
        DatabaseSettings.from_mapping(
            {
                "DIRECTPILOT_DATABASE_URL": (
                    "postgresql+psycopg://app:synthetic@127.0.0.1:1/directpilot"
                )
            }
        )
    )

    try:
        with pytest.raises(DatabaseConnectionError, match="database connection is unavailable") as captured:
            check_database_connection(runtime)
    finally:
        runtime.close()

    assert captured.value.__cause__ is None


def test_database_runtime_hides_bound_parameters() -> None:
    runtime = create_database_runtime(
        DatabaseSettings.from_mapping(
            {
                "DIRECTPILOT_DATABASE_URL": (
                    "postgresql+psycopg://app:synthetic@127.0.0.1:5432/directpilot"
                )
            }
        )
    )

    try:
        assert runtime.engine.hide_parameters is True
    finally:
        runtime.close()
