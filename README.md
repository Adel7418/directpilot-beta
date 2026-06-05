# DirectPilot Beta

Standalone API-first beta application for safe Yandex Direct automation for small business.

## Boundary

DirectPilot is not part of Hermes. Hermes, another agent, a web UI, or any other model should use the same REST/JSON API. DirectPilot owns Yandex OAuth tokens, Direct/Metrica integrations, permissions, budget limits, approval pipeline, and audit log.

## MVP mode

Default mode is `mock`: no calls to Yandex and no write actions. Real/sandbox Yandex access is configured via local `.env` only.

## Current Yandex access check

Local credentials were copied from the user's Obsidian note into `.env` with `chmod 600`.

- Yandex OAuth token check: PASS via `https://login.yandex.ru/info?format=json`.
- Yandex Direct API `clients.get`: BLOCKED by Yandex with error code `58` / `Незавершенная регистрация` — the app access request must be completed in the Direct interface and approved before Direct API calls will work.
- Current local mode: `sandbox`; DirectPilot targets `https://api-sandbox.direct.yandex.com/json/v5` for Direct API checks.

See: `docs/yandex-access-status.md`.

## References

- `docs/references/elama-vs-promopult.md` — comparison of eLama and PromoPult: what to borrow for DirectPilot Beta and what to avoid in MVP.

## Quick start

```bash
uv sync
uv run pytest -q
uv run uvicorn app.main:app --reload
```

OpenAPI: http://127.0.0.1:8000/openapi.json

## Safety

- `.env` is ignored by git.
- OAuth tokens must not be logged.
- Write actions require approval and idempotency keys.
- Yandex Direct live writes are out of scope for the first baseline.
