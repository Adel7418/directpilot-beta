# P2 PostgreSQL backup and restore runbook

Scope: P2 audit, idempotency, campaign-draft, and semantic-package tables. Run export and restore only through approved secret injection; do not place a password or connection string in this document, shell history, `.env`, source control, or a receipt.

1. Quiesce writes for the approved maintenance window and run `pg_dump` with the schema-owner identity into encrypted approved storage.
2. Restore with `pg_restore --exit-on-error` into a separate restored database. Never restore over the source database during rehearsal.
3. Verify the restored database is distinct from the source and compare the row count plus a deterministic SHA-256 signature of allowlisted synthetic records.
4. Run schema compatibility with the runtime role before reopening the application. If any check fails, keep the source database unchanged and fail closed.
5. Keep a sanitized receipt containing only database labels, migration head, PostgreSQL server version number, record count, deterministic record signature, and status.

The reproducible isolated rehearsal uses PostgreSQL 17.7-bookworm and writes its safe receipt to `artifacts/p2-backup-restore-rehearsal.json` (ignored by Git):

```text
DIRECTPILOT_DOCKER_BIN=docker uv run pytest tests/integration/test_backup_restore.py -q
```

The rehearsal removes the dump and the separate restored database after verification. The receipt intentionally contains no credentials, connection strings, raw payloads, or audit metadata.
