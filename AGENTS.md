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

## Human Approval Contract (mandatory for all write operations)

Technical `approved=true` in a request body is NOT a self-approval by an agent.
For any **real Direct/Metrika write** (minuses, keywords, ads, moderation,
strategy, bids, budgets, time-targeting, autotargeting, goals, UTM, pause/resume,
live-create, bids modifiers, etc.) the agent/marketer/script MUST follow this
sequence:

1. **Read-only / dry-run preview** — gather current state via read endpoints and
   `dry_run=true` previews. Produce a concrete diff/impact summary (what entities,
   what will change, expected effect, risk).
2. **Show to user** — present the preview and diff in chat, ticket, or operator
   UI. Never skip this step.
3. **Get explicit user confirmation** — the user must explicitly approve the
   action in the conversation/ticket/UI. The agent must not interpret silence,
   an unrelated "ok", or a general instruction as approval for a specific write.
4. **Apply only after confirmation** — only then send the write with
   `approved=true`, `idempotency_key`, `dry_run=false`, and
   `DIRECTPILOT_MODE=live_write`.
5. **Readback** — verify what changed using the operation readback in the
   response or a dedicated follow-up read endpoint.

Marketers do NOT apply live writes themselves. They prepare recommendations and
read-only/dry-run outputs only. The apply step is performed by an operator or
orchestrator after explicit user approval.

## UTM workflow for external agents (summary)

UTM is the primary mechanism for tracking ad performance in Yandex Metrika.
Every external agent (marketing, coding, reviewer) must follow this workflow:

**Existing campaigns:**
1. `GET /yandex/campaigns/{campaign_id}/utm-audit` — read-only audit
2. `POST /yandex/campaigns/{campaign_id}/utm-plan` — always dry_run, preview only; preserves query/fragment and emits concrete old_url → new_url
3. **User approval** (see Human Approval Contract above) — confirm slug, overwrite, sitelinks
4. `POST /yandex/campaigns/{campaign_id}/utm-apply` — with gates
5. Readback — verify confirmed URLs from response

**New campaigns (live-create):**
- Include `utm_config` in the `POST /yandex/campaigns/live-create` request to
  tag ads with UTM at birth.
- `LiveCreateCampaignRequest.utm_config` overrides `draft.utm_config` when both
  are set — the request-level config takes precedence.
- If `utm_config` is omitted, the draft's stored `utm_config` is used as fallback.

**When to ask the user:**
- Campaign slug/naming is unclear.
- **Cyrillic campaign names:** when the campaign name contains only Cyrillic
  characters, the automatic slug generator falls back to ``campaign-{id}``
  because transliteration drops non-ASCII chars.  For readable Metrika reports,
  ask the user to provide an explicit ``campaign_slug`` (Latin transliteration
  or semantic slug, e.g. ``turbiny-rostov`` instead of ``campaign-12345``).
- Overwrite of existing UTM is requested.
- Sitelinks UTM apply is desired; with `include_sitelinks=true`, quick-link URLs are updated through gated `utm-apply`/`sitelinks.update` and must be verified via `sitelink_readback`.

See `docs/MARKETER_GUIDE.md` (UTM section) and `docs/API_SIMPLE.md`
for the full endpoint contracts.

## Public skill vs private local skills
- This repository is self-contained for external humans and agents. Start with `AGENTS.md`, then read `skills/directpilot-operations/SKILL.md` and the docs listed above.
- Private Hermes skills such as a maintainer's local `yandex-direct-api` skill are convenience memory for that maintainer only. They are not required to operate or contribute to this repository.
- If a local/private skill discovers a reusable DirectPilot rule, copy the sanitized rule into repo docs or `skills/directpilot-operations/SKILL.md` so a fresh clone has the same operational knowledge.
- Never copy private memories, local paths, tokens, OAuth headers, account credentials, or customer secrets into the repo skill.

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

