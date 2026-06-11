# AGENTS.md

Purpose: quick onboarding for external humans and generic agents (including coding, marketing, or reviewer-like agents) without private Hermes memory.

## Source of truth (for this repository)
- `README.md` — project entry points and default safety posture.
- `CONTRIBUTING.md` — contribution workflow and expected checks.
- `docs/TECHNICAL_CONTEXT.md` — runtime mode map and operational constraints.
- `docs/implementation_scope.md` — product scope, included/excluded features, and API boundaries.
- `docs/API_SIMPLE.md` — practical endpoint map for implementation/consumers.
- `docs/MARKETER_GUIDE.md` — marketer task routing to DirectPilot endpoints.
- `docs/SAFETY_MODEL.md` — authoritative write-gate model.
- `docs/AGENT_WORKFLOWS.md` — role split and agent conduct.
- `skills/directpilot-operations/SKILL.md` — sanitized execution playbook.
- `.env.example` — safe local configuration template.
- `docs/YANDEX_TOKENS.md` — safe local setup for Yandex Direct OAuth, Metrika token, and Search/Wordstat API key.
- `docs/openapi.json` and runtime `GET /openapi.json` — machine-readable contract.

## Safety model (mandatory)
- Default operational mode is `live_readonly`.
- Real writes are blocked unless all conditions are true:
  1. `DIRECTPILOT_MODE=live_write`.
  2. `approved=true` in request.
  3. `idempotency_key` is provided and valid.
  4. `dry_run=false` for mutation behavior.
- `dry_run=true` is preview-only; it must not mutate external state.
- Do not read/search print/store secrets, raw OAuth tokens, API keys, cookies, auth headers, account credentials, customer data, or tokens.

## Routing rule for operations
- Marketing / semantic tasks: use `docs/MARKETER_GUIDE.md` and corresponding DirectPilot endpoints.
- If a direct Yandex API method exists in DirectPilot, **do not** call raw Yandex endpoints (`api.direct.yandex.com`, `api-metrika.yandex.net`, AI Studio/Search API) directly.

## Code-change rule
- Any code change affecting routes/models/safety/secrets/CI/openapi must include evidence:
  - `uv run pytest -q`
  - `uv run python scripts/check_openapi_sync.py`
  - `uv run python scripts/check_no_secrets_in_git.py`
- PR descriptions should include real command output for checks run.

## What should be avoided here
- No private local path assumptions (e.g., `/home/flora`, `.env`, host-specific secrets).
- No hidden behavior: if behavior changed, point to source docs and validation.
- No direct production writes without explicit gates above.

