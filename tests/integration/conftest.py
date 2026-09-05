from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
_COMPOSE = _WORKTREE / "deploy" / "compose.test.yml"
_DOCKER = os.environ.get("DIRECTPILOT_DOCKER_BIN", "docker")
_OWNER_PASSWORD = "synthetic-test-owner-password"
_APP_PASSWORD = "synthetic-test-app-password"


@dataclass(frozen=True, slots=True)
class PostgresService:
    port: int
    owner_url: str = field(repr=False)
    app_url: str = field(repr=False)
    app_password: str = field(repr=False)
    command_prefix: tuple[str, ...] = field(repr=False)


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run_compose(arguments: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=_WORKTREE,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture
def postgres_service(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[PostgresService, None, None]:
    port = _available_loopback_port()
    project_name = f"directpilot-p2-{port}"
    environment_file = tmp_path_factory.mktemp("postgres-compose") / "compose.env"
    environment_file.write_text(
        "\n".join(
            (
                "DIRECTPILOT_POSTGRES_DB=directpilot_test",
                f"DIRECTPILOT_POSTGRES_OWNER_PASSWORD={_OWNER_PASSWORD}",
                f"DIRECTPILOT_POSTGRES_PORT={port}",
                "",
            )
        ),
        encoding="utf-8",
    )
    command_prefix = [
        _DOCKER,
        "compose",
        "--project-name",
        project_name,
        "--env-file",
        str(environment_file),
        "-f",
        str(_COMPOSE),
    ]
    start = _run_compose([*command_prefix, "up", "--detach", "--wait", "--wait-timeout", "90"])
    if start.returncode != 0:
        _run_compose([*command_prefix, "down", "--volumes", "--remove-orphans"])
        pytest.fail(f"isolated PostgreSQL startup failed (exit {start.returncode})")

    service = PostgresService(
        port=port,
        owner_url=(
            "postgresql+psycopg://directpilot_owner:"
            f"{_OWNER_PASSWORD}@127.0.0.1:{port}/directpilot_test"
        ),
        app_url=(
            "postgresql+psycopg://directpilot_app:"
            f"{_APP_PASSWORD}@127.0.0.1:{port}/directpilot_test"
        ),
        app_password=_APP_PASSWORD,
        command_prefix=tuple(command_prefix),
    )
    try:
        yield service
    finally:
        stop = _run_compose([*command_prefix, "down", "--volumes", "--remove-orphans"])
        if stop.returncode != 0:
            pytest.fail(f"isolated PostgreSQL cleanup failed (exit {stop.returncode})")


@pytest.fixture
def postgres_url(postgres_service: PostgresService) -> str:
    return postgres_service.owner_url


@pytest.fixture
def restart_postgres(postgres_service: PostgresService) -> Callable[[], None]:
    def restart() -> None:
        restarted = _run_compose([*postgres_service.command_prefix, "restart", "postgres"])
        if restarted.returncode != 0:
            pytest.fail(f"isolated PostgreSQL restart failed (exit {restarted.returncode})")
        ready = _run_compose(
            [
                *postgres_service.command_prefix,
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "90",
            ]
        )
        if ready.returncode != 0:
            pytest.fail(f"isolated PostgreSQL readiness failed (exit {ready.returncode})")

    return restart
