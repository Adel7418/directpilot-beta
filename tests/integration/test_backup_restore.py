from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.modules.audit.repository import PostgresAuditRepository


_ROOT = Path(__file__).resolve().parents[2]
_RECEIPT_PATH = _ROOT / "artifacts" / "p2-backup-restore-rehearsal.json"
_SOURCE_DATABASE = "directpilot_test"
_RESTORED_DATABASE = "directpilot_restore"


def test_isolated_postgresql_backup_restore_preserves_synthetic_audit_invariants(
    postgres_service: object,
) -> None:
    """Exercise a real isolated dump and restore without retaining connection data."""

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
    command_prefix = list(getattr(postgres_service, "command_prefix"))
    dump_path = "/tmp/directpilot-p2-backup-restore.dump"
    _RECEIPT_PATH.unlink(missing_ok=True)

    def execute(stage: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        shell_command = (
            'export PGPASSWORD="$POSTGRES_PASSWORD"; exec '
            f"{shlex.join(arguments)}"
        )
        try:
            return subprocess.run(
                [
                    *command_prefix,
                    "exec",
                    "-T",
                    "postgres",
                    "sh",
                    "-ec",
                    shell_command,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
        except subprocess.TimeoutExpired as error:
            raise AssertionError(f"isolated PostgreSQL {stage} exceeded 45 seconds") from error

    def signature(database: str) -> tuple[int, str]:
        count = execute(
            f"{database} audit count",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "directpilot_owner",
            "-d",
            database,
            "-tAc",
            "SELECT count(*) FROM audit_events",
        )
        assert count.returncode == 0
        rows = execute(
            f"{database} audit signature",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "directpilot_owner",
            "-d",
            database,
            "-tA",
            "-c",
            (
                "SELECT id::text || '|' || actor || '|' || action || '|' || entity "
                "|| '|' || dry_run::text || '|' || COALESCE(details::text, '') "
                "FROM audit_events ORDER BY id"
            ),
        )
        assert rows.returncode == 0
        return int(count.stdout.strip()), hashlib.sha256(rows.stdout.encode()).hexdigest()

    try:
        assert _SOURCE_DATABASE != _RESTORED_DATABASE
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        PostgresAuditRepository(app_runtime.sessions).append_audit(
            "backup_restore_rehearsal",
            "synthetic-record",
            details={"request_id": "backup-restore-001", "mode": "mock"},
        )
        source_count, source_hash = signature(_SOURCE_DATABASE)
        assert source_count == 1

        dumped = execute(
            "dump",
            "pg_dump",
            "-U",
            "directpilot_owner",
            "-d",
            _SOURCE_DATABASE,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            f"--file={dump_path}",
        )
        assert dumped.returncode == 0
        removed = execute(
            "remove stale restore database",
            "dropdb",
            "-U",
            "directpilot_owner",
            "--if-exists",
            "--force",
            _RESTORED_DATABASE,
        )
        assert removed.returncode == 0
        created = execute(
            "create restore database",
            "createdb",
            "-U",
            "directpilot_owner",
            _RESTORED_DATABASE,
        )
        assert created.returncode == 0
        restored = execute(
            "restore",
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "-U",
            "directpilot_owner",
            "-d",
            _RESTORED_DATABASE,
            dump_path,
        )
        assert restored.returncode == 0
        restored_count, restored_hash = signature(_RESTORED_DATABASE)
        assert restored_count == source_count
        assert restored_hash == source_hash

        version = execute(
            "server version",
            "psql",
            "-U",
            "directpilot_owner",
            "-d",
            _SOURCE_DATABASE,
            "-tAc",
            "SHOW server_version_num",
        )
        assert version.returncode == 0
        _RECEIPT_PATH.parent.mkdir(exist_ok=True)
        _RECEIPT_PATH.write_text(
            json.dumps(
                {
                    "database_source": _SOURCE_DATABASE,
                    "database_restored": _RESTORED_DATABASE,
                    "migration_head": "20260904_0003",
                    "postgresql_server_version_num": int(version.stdout.strip()),
                    "record_count": source_count,
                    "record_signature_sha256": source_hash,
                    "schema": "directpilot.p2.backup-restore-rehearsal.v1",
                    "status": "passed",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        assert _RECEIPT_PATH.is_file()
    finally:
        execute(
            "remove restore database",
            "dropdb",
            "-U",
            "directpilot_owner",
            "--if-exists",
            "--force",
            _RESTORED_DATABASE,
        )
        execute("remove dump", "rm", "-f", dump_path)
        owner_runtime.close()
        app_runtime.close()
