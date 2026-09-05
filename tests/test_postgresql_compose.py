from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_WORKTREE = Path(__file__).resolve().parents[1]
_COMPOSE = _WORKTREE / "deploy" / "compose.test.yml"
_DOCKER = os.environ.get("DIRECTPILOT_DOCKER_BIN", "docker")


def test_test_compose_resolves_pinned_postgres_on_loopback_only(tmp_path: Path) -> None:
    environment_file = tmp_path / "compose.env"
    environment_file.write_text(
        "\n".join(
            (
                "DIRECTPILOT_POSTGRES_DB=directpilot_test",
                "DIRECTPILOT_POSTGRES_OWNER_PASSWORD=synthetic-test-password",
                "DIRECTPILOT_POSTGRES_PORT=55432",
                "",
            )
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            _DOCKER,
            "compose",
            "--env-file",
            str(environment_file),
            "-f",
            str(_COMPOSE),
            "config",
            "--format",
            "json",
        ],
        cwd=_WORKTREE,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    postgres = json.loads(result.stdout)["services"]["postgres"]
    assert postgres["image"] == "postgres:17.7-bookworm"
    assert postgres["ports"][0]["host_ip"] == "127.0.0.1"
    assert str(postgres["ports"][0]["published"]) == "55432"
