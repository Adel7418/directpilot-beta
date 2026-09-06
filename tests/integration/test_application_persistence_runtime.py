from __future__ import annotations

import pytest

from app.bootstrap import dependencies as dependencies_module
from app.bootstrap.dependencies import create_application_dependencies
from app.config import Settings
from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.repositories.postgres_store import PostgresLegacyStoreRepository


def test_production_dependencies_use_postgresql_repository_after_schema_readiness(
    monkeypatch: pytest.MonkeyPatch,
    postgres_service: object,
) -> None:
    owner_runtime = create_database_runtime(
        DatabaseSettings.from_mapping(
            {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "owner_url")}
        )
    )
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        monkeypatch.setenv("DIRECTPILOT_APP_ENV", "production")
        monkeypatch.setenv("DIRECTPILOT_DATABASE_URL", getattr(postgres_service, "app_url"))
        monkeypatch.setattr(
            dependencies_module,
            "get_settings",
            lambda: Settings(
                _env_file=None,
                yandex_client_id=None,
                yandex_client_secret=None,
                yandex_oauth_token=None,
                credential_keyring_secret_file=None,
            ),
        )

        dependencies = create_application_dependencies()
        try:
            assert isinstance(dependencies.repository, PostgresLegacyStoreRepository)
            assert dependencies.database_runtime is not None
        finally:
            if dependencies.database_runtime is not None:
                dependencies.database_runtime.close()
    finally:
        owner_runtime.close()
