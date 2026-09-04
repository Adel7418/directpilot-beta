from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Self

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL_ENV = "DIRECTPILOT_DATABASE_URL"


class DatabaseConfigurationError(ValueError):
    """Raised when PostgreSQL configuration is absent or unsafe."""


class DatabaseConnectionError(RuntimeError):
    """Raised when a database connection cannot be established safely."""


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Validated, non-displayable configuration for the synchronous database layer."""

    url: URL = field(repr=False)
    application_name: str = "directpilot"
    pool_size: int = 5
    connect_timeout_seconds: int = 5

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> Self:
        raw_url = values.get(DATABASE_URL_ENV)
        if raw_url is None or not raw_url.strip():
            raise DatabaseConfigurationError("database configuration is required")

        try:
            url = make_url(raw_url)
        except ArgumentError as exc:
            raise DatabaseConfigurationError("database configuration is invalid") from exc

        if url.drivername != "postgresql+psycopg":
            raise DatabaseConfigurationError("database configuration must use PostgreSQL psycopg")
        if not url.host or not url.database:
            raise DatabaseConfigurationError("database configuration is incomplete")

        return cls(url=url)

    @classmethod
    def from_environment(cls) -> Self:
        return cls.from_mapping(os.environ)


@dataclass(frozen=True, slots=True)
class DatabaseRuntime:
    """Synchronous engine and session factory owned by the application lifetime."""

    engine: Engine = field(repr=False)
    sessions: sessionmaker[Session] = field(repr=False)

    def close(self) -> None:
        self.engine.dispose()


def create_database_runtime(settings: DatabaseSettings) -> DatabaseRuntime:
    """Create one pre-pinged SQLAlchemy 2 runtime without opening a connection."""

    engine = create_engine(
        settings.url,
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=settings.pool_size,
        max_overflow=0,
        connect_args={
            "application_name": settings.application_name,
            "connect_timeout": settings.connect_timeout_seconds,
        },
    )
    return DatabaseRuntime(engine=engine, sessions=sessionmaker(bind=engine, expire_on_commit=False))


def check_database_connection(runtime: DatabaseRuntime) -> int:
    """Return the PostgreSQL server version or fail without rendering connection data."""

    try:
        with runtime.engine.connect() as connection:
            value = connection.execute(text("SHOW server_version_num")).scalar_one()
        return int(value)
    except (SQLAlchemyError, TypeError, ValueError):
        raise DatabaseConnectionError("database connection is unavailable") from None
