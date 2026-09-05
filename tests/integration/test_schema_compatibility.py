from __future__ import annotations

import pytest

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.db.schema import (
    SchemaCompatibilityError,
    check_schema_compatibility,
    expected_schema_revision,
)


def test_runtime_schema_readiness_fails_closed_until_owner_migrates(
    postgres_service: object,
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
        with pytest.raises(SchemaCompatibilityError, match="database schema is incompatible"):
            check_schema_compatibility(app_runtime)

        upgrade_database(owner_runtime)

        assert check_schema_compatibility(app_runtime) == expected_schema_revision()
    finally:
        owner_runtime.close()
        app_runtime.close()
