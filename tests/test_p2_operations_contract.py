from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def _contains_all(path: Path, *markers: str) -> bool:
    return path.is_file() and all(marker in path.read_text(encoding="utf-8") for marker in markers)


def test_p2_migration_ci_uses_real_postgresql_and_both_upgrade_paths() -> None:
    workflow = _ROOT / ".github" / "workflows" / "p2-postgresql-migrations.yml"

    assert _contains_all(
        workflow,
        "postgres:17.7-bookworm",
        "DIRECTPILOT_DOCKER_BIN: docker",
        "test_owner_migrates_empty_database_and_runtime_role_is_nonowner",
        "test_owner_upgrades_from_prior_p2_revision_to_current_head",
    )


def test_p2_operations_runbooks_reference_sanitized_rehearsal_evidence() -> None:
    migration_runbook = _ROOT / "docs" / "operations" / "migrations.md"
    restore_runbook = _ROOT / "docs" / "operations" / "backup-restore.md"
    dependency_admission = _ROOT / "docs" / "operations" / "dependency-admission.md"

    assert _contains_all(
        migration_runbook,
        "schema-owner",
        "empty database",
        "prior P2 revision",
        "head",
    )
    assert _contains_all(
        restore_runbook,
        "pg_dump",
        "pg_restore",
        "separate restored database",
        "artifacts/p2-backup-restore-rehearsal.json",
    )
    assert _contains_all(
        dependency_admission,
        "SQLAlchemy",
        "psycopg",
        "Alembic",
        "license",
        "vulnerability",
    )
