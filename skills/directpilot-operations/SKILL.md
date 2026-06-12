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
   - `GET /yandex/campaigns/{campaign_id}/ad-assets` — агрегированный аудит внешнего вида (заголовки, тексты, быстрые ссылки, организации, визитки)
   - `POST /yandex/ad-groups/{ad_group_id}/ads` — добавить объявления в существующую группу (dry_run default; apply — `live_write` + `approved` + `idempotency_key`)
   - `POST /yandex/ads/moderate` — отправить объявления на модерацию (dry_run default; apply — `live_write` + `approved` + `idempotency_key`)

Scope rule for operators/agents:

- If the user asks about a **specific campaign**, conclusions must come from campaign-scoped endpoints (`/yandex/campaigns/{campaign_id}/...`) or from account-wide data explicitly filtered through campaign/ad/ad-group ids.
- If the user asks about the **whole account**, account-wide endpoints are valid.
- Never infer that an account-wide entity is attached to a campaign merely because it exists in the account. Example: `GET /yandex/sitelinks` lists all sitelink sets; for campaign-specific quick links use `GET /yandex/campaigns/{campaign_id}/ad-assets` and its `SitelinkSetId`/`sitelinks_sets` readback.

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
8. For `/yandex/reports/search-queries`, the live contract mirrors `summary`:
   - `source="yandex"`, `read_only=true` in `sandbox`/`live_readonly`/`live_write` with a configured `YANDEX_OAUTH_TOKEN` and available Yandex client
   - source: `SEARCH_QUERY_PERFORMANCE_REPORT` v5 reports, fields `Query / CampaignId / AdGroupId / Impressions / Clicks / Ctr / Cost`
   - optional query params: `date_from`, `date_to` (YYYY-MM-DD), `campaign_id`
   - an empty live report is a valid response: `items=[]` with `source="yandex"`, NOT a mock fallback and NOT a 502
   - `source="mock"` is reserved for `DIRECTPILOT_MODE=mock` only — never silent in live modes
   - HTTP 409 in `sandbox`/`live_readonly`/`live_write` if the Yandex client/token is unavailable
   - Reports API pitfall: campaign filter MUST be sent as
     `SelectionCriteria.Filter = [{Field: "CampaignId", Operator: "IN", Values: ["..."]}]`,
     NOT as `SelectionCriteria.CampaignIds` (the latter returns HTTP 400 for the reports endpoint).
9. Use Wordstat endpoints for demand, seasonality, regions, and semantic expansion. See `docs/MARKETER_GUIDE.md` for the endpoint map.
10. Separate facts from hypotheses in the final answer:

   - facts from DirectPilot endpoints;
   - marketing interpretation;
   - recommended changes;
   - what requires explicit approval or implementation work.

For campaign creation or live mutation:

- Build drafts first through the documented DirectPilot draft/preview flow.
- Do not perform real Yandex writes unless the user explicitly approved the exact action.
- Keep `dry_run=true` for previews and safety checks.
- If DirectPilot lacks the required endpoint, report the product gap instead of bypassing DirectPilot with raw Yandex API calls.

When adding ads to an existing campaign/group via ``POST /yandex/ad-groups/{ad_group_id}/ads``:

- Ask or decide whether to reuse the existing BusinessId (default: reuse if existing ads already use one).
- Ask or decide whether to reuse the existing SitelinkSetId for quick links (default: reuse if relevant, verify via campaign-specific readback).
- Keywords and negative keywords are NOT per-ad — they are managed at campaign/ad-group level. If a new ad angle needs extra keywords/minuses, propose a separate semantic-change task.
- Technical and niche-specific phrasing in ad text is allowed as creative copy — do not treat it as the same as adding a key term in keyword targeting.
- If niche-specific terms can attract off-intent/DIY traffic, flag the risk and handle mitigation via separate keyword/minus workflows.
- After adding ads (or after live-create), send them to moderation via ``POST /yandex/ads/moderate`` — do NOT use ``campaigns.resume`` for new DRAFT campaigns.

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
- The campaign-level hourly schedule (TimeTargeting) can be read via the read-only `GET /yandex/campaigns/{campaign_id}/time-targeting` (no write gate; available in all modes including `mock`/`sandbox`/`live_readonly`/`live_write`). To update the schedule, use the write endpoint `POST /yandex/campaigns/{campaign_id}/time-targeting` (gate contract identical to the rest of the product surface: `approved` + `idempotency_key` + `dry_run`; `live_readonly` blocks real writes with HTTP 409 before any network call; real apply only in `live_write`).
- Before apply, the endpoint reads the current campaign `DailyBudget` via `campaigns.get`; if a daily budget exists, it includes it (with `SpendMode` normalized to `Mode`) in the `campaigns.update` payload — Direct v5 returns error_code=8000 ("Отсутствует обязательный параметр Mode") when `DailyBudget.Mode` is missing from an update on a campaign with a daily budget.
- If the `DailyBudget` read yields `null` (типичная картинка для text smart-strategies), endpoint does a second `campaigns.get` with `TextCampaignFieldNames` to read `Type` + `TextCampaign.BiddingStrategy`. For `TEXT_CAMPAIGN` cases, this strategy block is required in update payload together with `TimeTargeting`; missing strategy is treated as fail-closed and returns 502 before `campaigns.update`. `BudgetType` from read-side **must be preserved** in the write-side payload for `WbMaximumConversionRate` / `WbMaximumClicks` — the live API returns it on GET and requires it on UPDATE; stripping `BudgetType` causes error_code=8000.
- If the `DailyBudget` or strategy read fails/returns an ambiguous shape, apply is rejected with 502 (fail closed; no invented budget/strategy values). A successful `DailyBudget: null` read means there is no daily budget to preserve.
- Always use `dry_run=true` first to confirm the v5 payload preview; the apply path follows up with `campaigns.get TimeTargeting` for a read-back so the operator can diff `readback.TimeTargeting` against `schedule_applied`.

### Strategy management (BiddingStrategy)

- Current strategy can be read via `GET /yandex/campaigns/{campaign_id}/strategy` — read-only, no write gate, available in all modes. Returns `Type`, `State`, `Status`, `DailyBudget`, `CounterIds`, `TextCampaign.BiddingStrategy` (raw) and `strategy_summary` (normalized with micros→rubles conversion).
- To update strategy, use `POST /yandex/campaigns/{campaign_id}/strategy` — gate contract identical to time-targeting: `approved` + `idempotency_key` + `dry_run`; `live_readonly` blocks real writes with HTTP 409; real apply only in `live_write`.
- Live apply first reads campaign `DailyBudget` from `campaigns.get` and requires an unambiguous read for mode-dependent shape mapping. If `DailyBudget` cannot be reliably extracted in a supported shape, the request is rejected before `campaigns.update` with fail-closed 502 (no invented budget block).
- Currently supports switching search to `WB_MAXIMUM_CONVERSION_RATE` with a single `goal_id`, `weekly_spend_limit` (RUBLES, converted to micros × 1 000 000), optional `bid_ceiling` (RUBLES). This is a **replacement** of the strategy's current `GoalId`, not an append/add-to-list operation.
- The current DirectPilot endpoint does not accept `goal_ids: []` or “optimize for all goals”. If callers need multiple optimization goals, treat that as a product/API investigation first; do not imply that repeated calls accumulate goals.
- `BudgetType` (e.g. `WEEKLY_BUDGET`) is preserved from readback strategy block. Direct requires it on update; stripping it caused live `error_code=8000`.
- Network strategy defaults to preserve-from-readback. Explicit `network="SERVING_OFF"` is supported. Endpoint never silently turns networks ON.
- Before applying, read current campaign state via the GET endpoint to verify the single selected `goal_id` against `/metrika/counters/{counter_id}/goals`.

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
- Direct can return warning `10165` / `Параметр не будет применен`: one of the request fields was ignored by the API. The `details` field names the specific parameter. Check `provider_warnings` in the DirectPilot response to find which parameter was dropped.
- Reports API v5 (`/reports`) uses a different filter shape than the entity services. Campaign filters MUST be sent as `SelectionCriteria.Filter = [{Field: "CampaignId", Operator: "IN", Values: ["..."]}]`, NOT as `SelectionCriteria.CampaignIds` (the latter returns HTTP 400 on the reports endpoint — that field shape belongs to many JSON v5 entity services like `adgroups.get` / `ads.get` / `keywords.get`, not to `reports`). `SEARCH_QUERY_PERFORMANCE_REPORT`, `CAMPAIGN_PERFORMANCE_REPORT`, `ADGROUP_PERFORMANCE_REPORT`, `AD_PERFORMANCE_REPORT`, `CRITERIA_PERFORMANCE_REPORT` all share this contract.
- Reports API v5 can also return HTTP 400 `error_code=4000` when the same `ReportName` is reused with different parameters, e.g. different fields, date range, or filters: `Отчет с таким названием, но с отличающимися параметрами уже сформирован или находится в очереди. Измените значение в параметре ReportName`. Generate a deterministic unique `ReportName` per report definition, for example by appending a short stable hash of `ReportType + SelectionCriteria + FieldNames`.

## Verification checklist

- [ ] Tests pass.
- [ ] `live_readonly` still blocks writes.
- [ ] `dry_run=true` remains non-mutating.
- [ ] OpenAPI is synchronized if routes changed.
- [ ] No secrets or private customer data were committed.
