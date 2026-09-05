# P2 PostgreSQL migration runbook

Scope: P2 schema only. Run this procedure with the schema-owner connection supplied by the deployment secret mechanism. Do not use the runtime app role, a local `.env` file, or a connection value copied into a terminal transcript.

1. Confirm the target is an empty database or a known P2 revision. The migration owner is `directpilot_owner`; the runtime role remains non-owner and has no schema-changing privileges.
2. Take an approved backup before changing a non-empty database. Follow `docs/operations/backup-restore.md`.
3. In the release runner, construct the owner `DatabaseRuntime` from the securely injected owner configuration and call `app.db.migrations.runner.upgrade_database(owner_runtime, "head")`.
4. Run `app.db.schema.check_schema_compatibility(app_runtime)` with the runtime app role. A missing, outdated, or incompatible revision must fail closed; do not start the application in that state.
5. Record only revision identifiers and pass/fail status. Do not record URLs, passwords, tokens, or SQL parameter values.

Pre-release proof on an isolated PostgreSQL 17.7-bookworm container:

```text
DIRECTPILOT_DOCKER_BIN=docker uv run pytest \
  tests/integration/test_migrations_and_roles.py::test_owner_migrates_empty_database_and_runtime_role_is_nonowner -q
DIRECTPILOT_DOCKER_BIN=docker uv run pytest \
  tests/integration/test_migrations_and_roles.py::test_owner_upgrades_from_prior_p2_revision_to_current_head -q
```

The first command proves an empty database install. The second proves upgrade from the prior P2 revision to head. Both commands create and remove an isolated loopback-only Compose service.
