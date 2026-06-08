# DirectPilot Beta

DirectPilot is an API-first backend service for safe interaction with Yandex Direct, Yandex Metrika, and Wordstat. It is designed for programmatic integration only (REST/JSON API) and stores no business logic in external UIs.

## Current product mode

- Orientation: **live-first**
- Active testing/runtime mode: **`live_readonly`**
- Behavior: production Yandex data is read in real-time; write operations are blocked unless explicitly enabled.

## Quick start

```bash
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- Local OpenAPI: `http://127.0.0.1:8000/openapi.json`
- Static spec: `docs/openapi.json`

## Primary docs

- `docs/TECHNICAL_CONTEXT.md` — technical mode/controls/modes, live-write gates, retired routes, and source-of-truth map
- `docs/API_SIMPLE.md` — practical API usage map
- `docs/MARKETER_GUIDE.md` — marketer workflow map to DirectPilot endpoints
- `docs/implementation_scope.md` — current scope, in/out boundaries, and safety assumptions
- `docs/yandex-access-status.md` — latest Yandex access verification snapshot

## Safety notes

- No secrets are committed in docs or repo (secrets in local `.env` only).
- In `live_readonly`, external write-style actions are intentionally blocked.
- Controlled write execution requires the explicit write gate in runtime configuration and audit approval checks.
