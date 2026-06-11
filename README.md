# DirectPilot Beta

DirectPilot is an API-first backend service for safe interaction with Yandex Direct, Yandex Metrika, and Wordstat. It is designed for programmatic integration only (REST/JSON API) and stores no business logic in external UIs.

This repository is also an AI-oriented implementation and playbook for safely using Yandex advertising APIs (Yandex Direct, Yandex Metrika, Wordstat) via REST/JSON, with strict read-only by default and explicit live-write gates for mutations.

> **Public beta:** DirectPilot is not production-stable yet. The default runtime mode is `live_readonly`; write-capable operations are blocked unless the operator explicitly enables the `live_write` gate and sends an approved, idempotent request.

## Current product mode

- Orientation: **live-first**
- Active testing/runtime mode: **`live_readonly`**
- Behavior: production Yandex data is read in real time; write operations are blocked unless explicitly enabled.
- Contribution model: public beta through GitHub issues and pull requests; `main`/`master` should be protected and never accept direct pushes.

## Safety model at a glance

DirectPilot is connected to advertising APIs, so every contribution must preserve these rules:

1. Real write operations require `DIRECTPILOT_MODE=live_write`.
2. Write requests require `approved=true`.
3. Write requests require an `idempotency_key` so retries do not duplicate actions.
4. `dry_run=true` must never mutate Yandex state.
5. `live_readonly` must reject real writes before any network mutation call.
6. Secrets must stay in local `.env` files only and must never appear in issues, PRs, logs, docs, tests, fixtures, or committed files.

See [`docs/SAFETY_MODEL.md`](docs/SAFETY_MODEL.md) for the full contribution contract.

## Quick start

```bash
cp .env.example .env
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- Local OpenAPI: `http://127.0.0.1:8000/openapi.json`
- Static spec: `docs/openapi.json`

## Local verification

Run the same core checks expected for pull requests:

```bash
uv sync
uv run pytest -q
uv run python scripts/check_openapi_sync.py
uv run python scripts/check_no_secrets_in_git.py
```

## Primary docs

- `AGENTS.md` — first-stop handoff for external humans and generic agents
- `.env.example` — safe local configuration template without secrets
- `docs/YANDEX_TOKENS.md` — how to obtain and verify local Yandex Direct/Metrika/Search credentials safely
- `docs/TECHNICAL_CONTEXT.md` — technical mode/controls/modes, live-write gates, retired routes, and source-of-truth map
- `docs/API_SIMPLE.md` — practical API usage map
- `docs/MARKETER_GUIDE.md` — marketer workflow map to DirectPilot endpoints
- `docs/implementation_scope.md` — current scope, in/out boundaries, and safety assumptions
- `docs/yandex-access-status.md` — latest Yandex access verification snapshot
- `docs/SAFETY_MODEL.md` — beta safety contract for contributors and maintainers
- `docs/AGENT_WORKFLOWS.md` — agent/skill workflow rules for documentation and code changes

## Contributing

Contributions are welcome during beta, but all changes must go through pull requests. Start with [`CONTRIBUTING.md`](CONTRIBUTING.md), use the issue templates, and include real test output in PRs.

For code changes that affect write paths, API clients, runtime modes, secrets, CI, or GitHub governance, expect maintainer review and additional safety evidence.

## License

Licensed under the Apache License, Version 2.0. See [`LICENSE`](LICENSE).
