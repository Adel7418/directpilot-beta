from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.db.engine import DatabaseRuntime
from app.db.roles import grant_runtime_schema_readiness

_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS = Path(__file__).resolve().parent


def migration_config() -> Config:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_MIGRATIONS))
    return config


def upgrade_database(runtime: DatabaseRuntime, revision: str = "head") -> None:
    """Upgrade with the schema-owner connection and grant only version visibility."""

    config = migration_config()
    with runtime.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
        grant_runtime_schema_readiness(connection)


def downgrade_database(runtime: DatabaseRuntime, revision: str = "base") -> None:
    """Downgrade under the owner connection for isolated migration verification."""

    config = migration_config()
    with runtime.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)
