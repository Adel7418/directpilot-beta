from __future__ import annotations

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.engine import DatabaseRuntime
from app.db.migrations.runner import migration_config


class SchemaCompatibilityError(RuntimeError):
    """Raised when the runtime role cannot prove the expected schema revision."""


def expected_schema_revision() -> str:
    revision = ScriptDirectory.from_config(migration_config()).get_current_head()
    if revision is None:
        raise SchemaCompatibilityError("database schema is incompatible")
    return revision


def check_schema_compatibility(runtime: DatabaseRuntime) -> str:
    """Fail closed unless the runtime role sees exactly the current migration head."""

    expected = expected_schema_revision()
    try:
        with runtime.engine.connect() as connection:
            actual = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except SQLAlchemyError:
        raise SchemaCompatibilityError("database schema is incompatible") from None

    if actual != expected:
        raise SchemaCompatibilityError("database schema is incompatible")
    return expected
