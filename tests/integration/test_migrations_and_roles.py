from __future__ import annotations

import pytest

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import downgrade_database, upgrade_database
from app.db.schema import (
    SchemaCompatibilityError,
    check_schema_compatibility,
    expected_schema_revision,
)
from app.db.roles import (
    bootstrap_database_roles,
    get_role_attributes,
    get_table_owner,
)


def test_owner_migrates_empty_database_and_runtime_role_is_nonowner(
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
        runtime_role = get_role_attributes(owner_runtime)

        assert runtime_role.name == "directpilot_app"
        assert runtime_role.is_superuser is False
        assert runtime_role.can_bypass_rls is False
        assert runtime_role.can_create_database is False
        assert runtime_role.can_create_role is False
        assert runtime_role.can_replicate is False
        assert get_table_owner(owner_runtime, "alembic_version") == "directpilot_owner"
    finally:
        owner_runtime.close()


def test_owner_upgrades_from_prior_p2_revision_to_current_head(
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
        upgrade_database(owner_runtime)
        downgrade_database(owner_runtime, revision="base")
        upgrade_database(owner_runtime, revision="20260904_0001")

        with pytest.raises(SchemaCompatibilityError, match="database schema is incompatible"):
            check_schema_compatibility(app_runtime)

        upgrade_database(owner_runtime)

        assert check_schema_compatibility(app_runtime) == expected_schema_revision()
        assert get_table_owner(owner_runtime, "idempotency_records") == "directpilot_owner"
    finally:
        owner_runtime.close()
        app_runtime.close()
