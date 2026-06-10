# Contributing to DirectPilot Beta

Thank you for helping improve DirectPilot. This project is in public beta and touches advertising APIs, so contributions are welcome only through a reviewable, test-backed workflow.

## Contribution workflow

1. Open or find an issue before starting large work.
2. Fork the repository and create a focused branch.
3. Keep the change small and explain the user-visible reason.
4. Run local checks.
5. Open a pull request using the PR template.
6. Wait for CI and maintainer review before merge.

Do not push directly to the protected branch. Maintainers should require passing checks and at least one approval before merge.

## Local setup

```bash
uv sync
cp .env.example .env
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Do not put real Yandex tokens in committed files. `.env` is ignored and must stay local.

## Required checks before PR

```bash
uv run pytest -q
uv run python scripts/check_openapi_sync.py
uv run python scripts/check_no_secrets_in_git.py
```

If you changed dependencies, also explain why in the PR body. Python dependency updates are manual during beta: run `uv lock --upgrade-package <name>` and commit both `pyproject.toml` and `uv.lock` together so CI can keep using `uv sync --frozen`.

## DirectPilot safety contract

Every PR must preserve these rules:

- `DIRECTPILOT_MODE=live_readonly` is the safe default.
- Real writes are allowed only in `live_write` mode.
- Write requests must require `approved=true`.
- Write requests must require an `idempotency_key`.
- `dry_run=true` must never mutate external Yandex state.
- `live_readonly` must reject writes before any network mutation call.
- Errors, logs, responses, tests, and docs must not leak OAuth tokens, API keys, client secrets, account IDs that are private, or real customer data.

Changes to these areas are high-risk and require maintainer attention:

- Yandex Direct/Metrika/Wordstat clients
- live-write gates and runtime mode handling
- budget/campaign/keyword/vCard/semantic-change operations
- authentication, tokens, config, and environment handling
- audit log behavior
- GitHub Actions, CODEOWNERS, and security tooling
- skills or agent instructions that could trigger live writes

## Documentation changes

Documentation must be accurate and source-backed. If a doc describes an endpoint, verify it against the app routes or OpenAPI spec. If a doc describes a safety behavior, point to the test or code path that enforces it.

## Commit style

Use short conventional-style subjects where practical:

```text
docs: add beta contribution policy
fix: block live write in readonly mode
test: cover semantic change idempotency
```

## Pull request checklist

A good PR includes:

- what changed;
- why it changed;
- test output;
- safety impact;
- screenshots or API examples if relevant;
- note whether OpenAPI changed.
