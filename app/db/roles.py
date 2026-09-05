from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db.engine import DatabaseRuntime

MIGRATION_ROLE = "directpilot_owner"
RUNTIME_ROLE = "directpilot_app"


class DatabaseRoleError(RuntimeError):
    """Raised when the required non-owner runtime role is unavailable."""


@dataclass(frozen=True, slots=True)
class RuntimeRoleAttributes:
    name: str
    is_superuser: bool
    can_bypass_rls: bool
    can_create_database: bool
    can_create_role: bool
    can_replicate: bool


def bootstrap_database_roles(runtime: DatabaseRuntime, *, app_password: str) -> None:
    """Create or reset the restricted runtime login under the schema owner."""

    if not app_password:
        raise DatabaseRoleError("runtime role password is required")

    with runtime.engine.begin() as connection:
        connection.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_roles WHERE rolname = 'directpilot_app'
                    ) THEN
                        CREATE ROLE directpilot_app
                            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
                    END IF;
                END
                $$;
                """
            )
        )
        connection.execute(
            text("SELECT set_config('directpilot.runtime_role_password', :password, true)"),
            {"password": app_password},
        )
        connection.execute(
            text(
                """
                DO $$
                BEGIN
                    EXECUTE format(
                        'ALTER ROLE directpilot_app WITH LOGIN NOSUPERUSER NOCREATEDB '
                        'NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L',
                        current_setting('directpilot.runtime_role_password', true)
                    );
                END
                $$;
                """
            )
        )
        connection.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
        connection.execute(text("GRANT USAGE ON SCHEMA public TO directpilot_app"))


def grant_runtime_schema_readiness(connection: Connection) -> None:
    """Permit the app role to read the Alembic version without schema ownership."""

    connection.execute(text("GRANT SELECT ON TABLE alembic_version TO directpilot_app"))


def get_role_attributes(runtime: DatabaseRuntime) -> RuntimeRoleAttributes:
    with runtime.engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication
                FROM pg_roles
                WHERE rolname = :role_name
                """
            ),
            {"role_name": RUNTIME_ROLE},
        ).mappings().one_or_none()

    if row is None:
        raise DatabaseRoleError("runtime role is missing")

    return RuntimeRoleAttributes(
        name=str(row["rolname"]),
        is_superuser=bool(row["rolsuper"]),
        can_bypass_rls=bool(row["rolbypassrls"]),
        can_create_database=bool(row["rolcreatedb"]),
        can_create_role=bool(row["rolcreaterole"]),
        can_replicate=bool(row["rolreplication"]),
    )


def get_table_owner(runtime: DatabaseRuntime, table_name: str) -> str:
    with runtime.engine.connect() as connection:
        owner = connection.execute(
            text(
                """
                SELECT tableowner
                FROM pg_tables
                WHERE schemaname = 'public' AND tablename = :table_name
                """
            ),
            {"table_name": table_name},
        ).scalar_one_or_none()

    if owner is None:
        raise DatabaseRoleError("required schema table is missing")
    return str(owner)
