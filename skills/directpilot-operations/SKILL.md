---
name: directpilot-operations
description: Safe beta operation and contribution workflow for DirectPilot, an API-first Yandex Direct/Metrika/Wordstat backend.
version: 0.1.0-beta.1
license: Apache-2.0
metadata:
  hermes:
    tags: [directpilot, yandex-direct, yandex-metrika, wordstat, beta, safety]
---

# DirectPilot Operations

Use this skill when working on DirectPilot code, docs, tests, or examples.

## Safety defaults

- Default runtime mode: `live_readonly`.
- Real writes require `DIRECTPILOT_MODE=live_write`.
- Write requests require `approved=true` and an `idempotency_key`.
- `dry_run=true` must never mutate Yandex state.
- Secrets stay in local `.env` only and must never be committed or pasted into issues/PRs.

## Workflow

1. Read `README.md`, `CONTRIBUTING.md`, `docs/SAFETY_MODEL.md`, and `docs/API_SIMPLE.md`.
2. For marketing/ads analysis, also read `docs/MARKETER_GUIDE.md` before making recommendations.
3. Identify whether the work is read-only analysis, docs-only, read-only behavior, or write-path behavior.
4. For docs-only changes, keep endpoint descriptions synchronized with OpenAPI.
5. For code changes, add or update tests before claiming completion.
6. Run:
   ```bash
   uv run pytest -q
   uv run python scripts/check_openapi_sync.py
   uv run python scripts/check_no_secrets_in_git.py
   ```
7. Include the executed commands and results in the PR.

## Marketing and campaign operations

This public skill is not only for developers. A generic agent or human operator should be able to use it to inspect and improve campaigns through DirectPilot without private Hermes skills.

For read-only marketing work:

1. Check integration status: `GET /health`, `GET /integrations/yandex/direct/status`.
2. List real campaigns: `GET /yandex/campaigns`.
3. For a selected campaign, inspect structure:
   - `GET /yandex/campaigns/{campaign_id}/ad-groups`
   - `GET /yandex/campaigns/{campaign_id}/ads`
   - `GET /yandex/campaigns/{campaign_id}/keywords`
4. Read performance:
   - `GET /yandex/reports/summary?campaign_id=...&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`
   - `GET /yandex/reports/search-queries` when available for intent/minus-keyword analysis.
5. Read money/budget signals:
   - `GET /yandex/account/balance`
   - `GET /yandex/campaigns/finance`
6. Read Metrika context:
   - `GET /metrika/counters`
   - `GET /metrika/counters/{counter_id}/goals`
   - `GET /metrika/counters/{counter_id}/summary`
   - `GET /metrika/counters/{counter_id}/traffic-sources`
7. For `/yandex/reports/summary`, use query filters explicitly when needed:
   - `campaign_id`
   - `date_from` (YYYY-MM-DD)
   - `date_to` (YYYY-MM-DD)
   - expected live behavior: `source="yandex"`, `read_only=true` in non-mock modes; `source="mock"` only in `DIRECTPILOT_MODE=mock`
   - expected fallback: HTTP 409 in `sandbox`/`live_readonly`/`live_write` if the Yandex client/token is unavailable
8. Use Wordstat endpoints for demand, seasonality, regions, and semantic expansion. See `docs/MARKETER_GUIDE.md` for the endpoint map.
9. Separate facts from hypotheses in the final answer:

   - facts from DirectPilot endpoints;
   - marketing interpretation;
   - recommended changes;
   - what requires explicit approval or implementation work.

For campaign creation or live mutation:

- Build drafts first through the documented DirectPilot draft/preview flow.
- Do not perform real Yandex writes unless the user explicitly approved the exact action.
- Keep `dry_run=true` for previews and safety checks.
- If DirectPilot lacks the required endpoint, report the product gap instead of bypassing DirectPilot with raw Yandex API calls.

## High-risk areas

- Yandex API clients
- semantic-change apply logic
- vCard/campaign/keyword write operations
- runtime mode handling
- audit logs and error messages
- CI/security workflows
- secrets and configuration

## Direct API operational pitfalls

- New DRAFT campaigns are not launched with `campaigns.resume`. Draft-to-moderation uses `ads.moderate` with `SelectionCriteria.Ids=[ad_ids]`; `campaigns.resume` is only for already-created stopped/suspended campaigns.
- For live bid updates through Direct v5 `keywordbids.set`, concrete known keywords should use the minimal item shape:
  ```json
  {"KeywordId": 57440007797, "SearchBid": 250000000}
  ```
  `SearchBid` is in Direct micros: `250000000` = `250 ₽`.
- Do not mix `CampaignId + AdGroupId + KeywordId + SearchBid` in each item for a concrete-keyword batch update. A live 30-keyword update returned `error_code=9300` for that form; retrying with `KeywordId + SearchBid` succeeded.
- For autotargeting rows, add `AutotargetingSearchBidIsAuto="NO"` when setting a manual search bid.
- Do not set `NetworkBid` when the campaign must remain search-only (`Network.BiddingStrategyType=SERVING_OFF`).
- Switching a text campaign from manual `HIGHEST_POSITION` to `WB_MAXIMUM_CONVERSION_RATE` uses `TextCampaign.BiddingStrategy.Search.WbMaximumConversionRate` with `GoalId`, `WeeklySpendLimit`, and optional `BidCeiling`; keep `Network.BiddingStrategyType=SERVING_OFF` for search-only campaigns.
- Direct can return warning `10162` / `Дневной бюджет сброшен` when switching to weekly conversion strategy. This is expected: `DailyBudget` is meaningful for manual strategies; the conversion strategy uses `WeeklySpendLimit`.
- When adding keywords under `WB_MAXIMUM_CONVERSION_RATE`, Direct can return warning `10160` / `Ставка не будет применена`: `Bid` is ignored by the auto-budget strategy, and `ContextBid` is ignored when Network is `SERVING_OFF`. This is expected; control spend through `WeeklySpendLimit` and `BidCeiling`.

## Verification checklist

- [ ] Tests pass.
- [ ] `live_readonly` still blocks writes.
- [ ] `dry_run=true` remains non-mutating.
- [ ] OpenAPI is synchronized if routes changed.
- [ ] No secrets or private customer data were committed.
