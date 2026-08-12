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
- Human approval contract for *all* write paths: preview/diff → explicit user confirmation → apply → readback.
- `approved=true` is a technical gate, not agent self-approval.
- Marketer/agent role never executes final live writes alone; they prepare recommendations and dry-run/preview outputs. Apply is done after explicit user confirmation by operator/orchestrator.
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
   - `GET /yandex/campaigns/{campaign_id}/ad-groups/negative-keywords`
   - `POST /yandex/campaigns/{campaign_id}/ad-groups/{ad_group_id}/negative-keywords`
   - `GET /yandex/campaigns/{campaign_id}/keyword-bids` — canonical typed `keywordbids.get` read; legacy `GET .../bids` is deprecated raw `bids.get`
   - `POST /yandex/campaigns/{campaign_id}/keyword-bids/set-auto` — `keywordbids.setAuto` bid calculation, dry-run default, gated apply; not autotargeting or strategy conversion
   - `GET /yandex/campaigns/{campaign_id}/bid-modifiers`
   - `POST /yandex/campaigns/{campaign_id}/bid-modifiers` (dry_run/apply)
   - `POST /yandex/campaigns/{campaign_id}/ad-groups`
   - `GET /yandex/campaigns/{campaign_id}/ad-assets` — агрегированный аудит внешнего вида (заголовки, тексты, быстрые ссылки, организации, визитки)
   - `POST /yandex/ad-groups/{ad_group_id}/ads` — добавить объявления в существующую группу (dry_run default; apply — `live_write` + `approved` + `idempotency_key`)
   - `POST /yandex/ads/moderate` — отправить объявления на модерацию (dry_run default; apply — `live_write` + `approved` + `idempotency_key`)
   - `GET /yandex/campaigns/{campaign_id}/autotargeting` — посмотреть настройки автотаргетинга (категории + brand-опции) каждой группы; read-only
   - `POST /yandex/campaigns/{campaign_id}/autotargeting` — обновить автотаргетинг (dry_run default; apply — `live_write` + `approved` + `idempotency_key`)
   - `GET /yandex/campaigns/{campaign_id}/utm-audit` — аудит UTM-разметки (read-only; статус по объявлениям и быстрым ссылкам)
   - `POST /yandex/campaigns/{campaign_id}/utm-plan` — план UTM (всегда dry_run; old_url → new_url preview; payload_preview для apply)
   - `POST /yandex/campaigns/{campaign_id}/utm-apply` — применить UTM (dry_run default; apply — `live_write` + `approved` + `idempotency_key`; `include_sitelinks=true` updates sitelinks via `sitelinks.update`)

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
   - source: `SEARCH_QUERY_PERFORMANCE_REPORT` v5 reports, fields `Query / CampaignId / CampaignName / AdGroupId / Impressions / Clicks / Ctr / Cost`
   - if CampaignName is missing in report rows, resolve name via `campaigns.get` by CampaignId
   - optional query params: `date_from`, `date_to` (YYYY-MM-DD), `campaign_id`
   - an empty live report is a valid response: `items=[]` with `source="yandex"`, NOT a mock fallback and NOT a 502
   - Use `/yandex/reports/search-queries-live?date_from=...&date_to=...` for raw TSV diagnostics; it must use the same search-query field set `Query, CampaignId, AdGroupId, Impressions, Clicks, Ctr, Cost`, not the generic campaign-summary defaults.
   - `source="mock"` is reserved for `DIRECTPILOT_MODE=mock` only — never silent in live modes
   - HTTP 409 in `sandbox`/`live_readonly`/`live_write` if the Yandex client/token is unavailable
   - Reports API pitfalls:
     - campaign filter MUST be sent as
       `SelectionCriteria.Filter = [{Field: "CampaignId", Operator: "IN", Values: ["..."]}]`,
       NOT as `SelectionCriteria.CampaignIds` (the latter returns HTTP 400 for the reports endpoint).
     - `ReportName` should be stable per contract (`ReportType + SelectionCriteria + FieldNames`) to avoid `error_code=4000` collisions.
   - Use campaign and group breakdown:
     1) Run base scope:
        `GET /yandex/reports/search-queries?date_from=...&date_to=...`.
     2) For candidate campaign, run:
        `GET /yandex/reports/search-queries?campaign_id=<id>&date_from=...&date_to=...`.
     3) Group by `campaign_id` / `campaign_name`, then by `ad_group_id`; compare `cost`, `impressions`, `clicks`, `ctr`.
     4) Build recommendation actions:
        - `keep` (query приносит клики/CTR и релевантен цели),
        - `add as key` (есть intent и стабильный traffic),
        - `minus` (высокий noise / низкая отдача),
        - `needs data` (слишком мало статистики).
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
- Keywords and negative keywords are NOT per-ad — they are managed at campaign/ad-group level.
  For group-level negative updates use:
  - `GET /yandex/campaigns/{campaign_id}/ad-groups/negative-keywords` — read/audit every group with `negative_keywords`, `has_negative_keywords`, `source`, `read_only`.
  - `POST /yandex/campaigns/{campaign_id}/ad-groups/{ad_group_id}/negative-keywords` — `operation=add|replace`, `dry_run=true` by default; real apply requires `DIRECTPILOT_MODE=live_write`, `approved=true`, `idempotency_key`, `dry_run=false`.
  - For `operation=add`, DirectPilot reads current `NegativeKeywords.Items`, strips leading `-`, dedupes preserving order, then sends merged `adgroups.update` because Yandex Direct treats `NegativeKeywords.Items` as replace-style.
  - Fail closed on provider business-error envelopes (`ok=false`) from pre-read or write calls: return HTTP 502 before constructing `applied=true` results or caching idempotency success. Never continue an add/merge after a failed `adgroups.get` pre-read because the write is replace-style.
  - `POST /yandex/campaigns/{campaign_id}/ad-groups` — create a new group in an existing campaign when structure must change first; `region_ids` is required and maps to Direct `RegionIds`; this endpoint creates only the group, not ads/keywords/moderation.
  If a new ad angle needs extra keywords/minuses, propose a separate semantic-change or ad-group operation task.

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

- Current strategy can be read via `GET /yandex/campaigns/{campaign_id}/strategy` — read-only, no write gate, available in all modes. Returns `Type`, `State`, `Status`, `DailyBudget`, `CounterIds`, `PriorityGoals`, `TextCampaign.BiddingStrategy` (raw) and `strategy_summary` (normalized with micros→rubles conversion).
- To update strategy, use `POST /yandex/campaigns/{campaign_id}/strategy` — gate contract identical to time-targeting: `approved` + `idempotency_key` + `dry_run`; `live_readonly` blocks real writes with HTTP 409; real apply only in `live_write`.
- Live apply first reads campaign `DailyBudget` from `campaigns.get` and requires an unambiguous read for mode-dependent shape mapping. If `DailyBudget` cannot be reliably extracted in a supported shape, the request is rejected before `campaigns.update` with fail-closed 502 (no invented budget block).
- Supports three mutually exclusive goal selection modes:
  - **Single goal**: `goal_id` (int, backward-compatible) — replaces the strategy's current `GoalId`.
  - **Multi-goal equal weight**: `goal_ids` (list[int], max 30, unique positive ids) — sets `WbMaximumConversionRate.GoalId=13` and populates `TextCampaign.PriorityGoals.Items` with equal default value 1.0 RUB per goal.
  - **Multi-goal explicit values**: `priority_goals` (list[`{goal_id:int, value:float|None}`], max 30, unique ids, values in RUBLES) — same Direct shape as `goal_ids` but with caller-specified per-goal conversion values. `value=None` defaults to 1.0 RUB.
- **Live contract pitfall — clearing vs setting PriorityGoals**: Direct API v5 clears an existing multi-goal set only with `TextCampaign.PriorityGoals=null`. Single-goal migration (`goal_id`) must send that explicit null to remove prior `PriorityGoals`; never use `PriorityGoals.Items=[]` and never omit `PriorityGoals`, because omission is not a clear contract.
- **Live contract pitfall — Operation field**: Direct API v5 `campaigns.update` requires every non-empty `PriorityGoals.Items[]` element to carry `"Operation": "SET"`. Omitting `Operation` on any item returns `error_code=8000` («отсутствует обязательное поле Operation»). DirectPilot adds this automatically for both `goal_ids` and `priority_goals` modes. External agents constructing payloads against this skill must include `"Operation": "SET"` on every item.
- `goal_id`, `goal_ids`, and `priority_goals` are mutually exclusive. Exactly one mode must be chosen.
- `weekly_spend_limit` and `bid_ceiling` are in RUBLES (public REST convention). The store converts to Direct micros (× 1 000 000).
- `BudgetType` (e.g. `WEEKLY_BUDGET`) is preserved from readback strategy block. Direct requires it on update; stripping it caused live `error_code=8000`. For strategy live apply, `DailyBudget=null` or a missing daily budget is acceptable only when readback confirms `Search.WbMaximumConversionRate.BudgetType="WEEKLY_BUDGET"`; otherwise DirectPilot fails closed before `campaigns.update`.
- Network strategy defaults to preserve-from-readback. Explicit `network="SERVING_OFF"` is supported. Endpoint never silently turns networks ON.
- Before applying, read current campaign state via the GET endpoint to verify the selected goal(s) against `/metrika/counters/{counter_id}/goals`.

### Keyword bids update (SearchBid / ContextBid)

- Use `POST /yandex/campaigns/{campaign_id}/bids` for safe live bid changes.
  - `dry_run=true` (default): preview-only, no network write. Returns `payload_preview`
    with exact v5 `keywordbids.set` payload.
  - `dry_run=false`: requires `DIRECTPILOT_MODE=live_write` + `approved=true` +
    `idempotency_key`. Returns `applied=true` with `readback` only on fully successful apply.
  - `approved=false` is rejected with HTTP 409 before any network call.
  - `live_readonly` blocks real writes with HTTP 409.
  - same `idempotency_key` with different `dry_run`/payload is rejected with HTTP 409 before network write.
  - top-level Yandex/`keywordbids.set` failure returns HTTP 502 with redacted diagnostics.
- Request items are in RUBLES at the REST boundary (`search_bid_rub`/`context_bid_rub`);
  the store converts to Direct micros (× 1 000 000).
- Minimal v5 item shape for known keyword ids: `KeywordId + SearchBid` /
  `KeywordId + ContextBid`. Do NOT include `CampaignId` / `AdGroupId` in
  the item — Direct returns `error_code=9300` for that form on batch updates.
- For autotargeting rows, `AutotargetingSearchBidIsAuto="NO"` is added
  automatically when setting a manual search bid. Set
  `autotargeting_search_bid_is_auto=true` explicitly to suppress.
- Do not set `NetworkBid` / `context_bid_rub` when the campaign must remain
  search-only (`Network.BiddingStrategyType=SERVING_OFF`).
- After fully successful apply, endpoint reads back keyword bids via `keywords.get` and returns
  `readback` with changed `KeywordId` + current `Bid`/`ContextBid` in Direct micros
  (250_000_000 = 250 ₽). On `partial_failure=true`, readback is not attempted.
- Idempotency: replay with same key returns cached result only when `dry_run` and
  payload/material item set are equivalent; same `idempotency_key` with a different
  payload (including different `dry_run`) is rejected with HTTP 409.
- Direct can return warning `10160` / `Ставка не будет применена` for
  auto-strategy campaigns (`WB_MAXIMUM_CONVERSION_RATE`): per-keyword bids
  are ignored. Control spend through `WeeklySpendLimit`/`BidCeiling` via
  `/yandex/campaigns/{campaign_id}/strategy`. Check `provider_warnings` in
  the response.
- Direct can return warning `10165` / `Параметр не будет применен`: one of the request fields was ignored by the API. The `details` field names the specific parameter. Check `provider_warnings` in the DirectPilot response to find which parameter was dropped.
- Reports API v5 (`/reports`) uses a different filter shape than the entity services. Campaign filters MUST be sent as `SelectionCriteria.Filter = [{Field: "CampaignId", Operator: "IN", Values: ["..."]}]`, NOT as `SelectionCriteria.CampaignIds` (the latter returns HTTP 400 on the reports endpoint — that field shape belongs to many JSON v5 entity services like `adgroups.get` / `ads.get` / `keywords.get`, not to `reports`). `SEARCH_QUERY_PERFORMANCE_REPORT`, `CAMPAIGN_PERFORMANCE_REPORT`, `ADGROUP_PERFORMANCE_REPORT`, `AD_PERFORMANCE_REPORT`, `CRITERIA_PERFORMANCE_REPORT` all share this contract.
- Reports API v5 can also return HTTP 400 `error_code=4000` when the same `ReportName` is reused with different parameters, e.g. different fields, date range, or filters: `Отчет с таким названием, но с отличающимися параметрами уже сформирован или находится в очереди. Измените значение в параметре ReportName`. Generate a deterministic unique `ReportName` per report definition, for example by appending a short stable hash of `ReportType + SelectionCriteria + FieldNames`.

### KeywordBids.get / setAuto

- Canonical read endpoint: `GET /yandex/campaigns/{campaign_id}/keyword-bids`
  (`keywordbids.get`). It returns typed `KeywordBids` rows, strategy-safe
  Search/Network fields, RUB + micros values, row classification
  (`keyword` / `autotargeting` / `unknown`), and `LimitedBy` pagination
  as `limited_by` / `next_offset`.
- Legacy read endpoint: `GET /yandex/campaigns/{campaign_id}/bids` is
  deprecated raw `bids.get` compatibility. Do not use it as the canonical
  marketer/operator bid read.
- Existing manual write endpoint: `POST /yandex/campaigns/{campaign_id}/bids`
  remains `keywordbids.set` for fixed SearchBid/ContextBid updates and keeps the
  standard gates and idempotency.
- Automatic bid calculation endpoint:
  `POST /yandex/campaigns/{campaign_id}/keyword-bids/set-auto` wraps
  `keywordbids.setAuto`. It calculates bids; it does **not** configure
  autotargeting categories, create/remove `---autotargeting`, convert campaign
  strategy, change payment model, or silently enable Search/Network.
- Request scopes are homogeneous: exactly one of `campaign`, `ad_group`, or
  `keyword`. `ad_group` accepts unique `ad_group_ids` up to 1,000. `keyword`
  accepts unique `keyword_ids` up to 10,000 and rejects autotargeting rows.
- Rules: `search_by_traffic_volume` requires `target_traffic_volume=5..100` and
  is compatible only with Search `HIGHEST_POSITION`; `network_by_coverage`
  requires `target_coverage=0..100` and is compatible only with Network
  `MAXIMUM_COVERAGE` or `MANUAL_CPM`. Both allow `increase_percent=0..1000` and
  require positive DirectPilot `bid_ceiling_rub`, converted to Direct micros in
  the preview (`RUB * 1,000,000`).
- `dry_run=true` is the default. It returns the exact `setAuto`
  `payload_preview`, affected items, warnings, and blockers without mutation.
  Pydantic/schema errors may be HTTP 422.
- Real apply requires the human approval contract plus `DIRECTPILOT_MODE=live_write`,
  `approved=true`, valid `idempotency_key`, and `dry_run=false`. Idempotency is
  based on the material `setAuto` payload; replay with a different payload/key
  meaning must be rejected before mutation.
- Fail closed before mutation on incompatible strategy, mixed selectors,
  ownership mismatch, keyword-scope autotargeting, missing/unknown target reads,
  or sanitized provider errors. Never auto-switch strategy to make `setAuto`
  compatible.
- Pre-write `campaign` and `ad_group` scopes remain fail-closed on any
  `LimitedBy`/truncation. A `keyword` scope is verifiable only when every
  requested `KeywordId` appears exactly once and each row's `CampaignId` matches
  the campaign in the URL; when those checks pass, `LimitedBy` alone does not
  block. Missing IDs return `setAuto readback does not contain requested keyword
  IDs: [...]`; duplicate, mismatched, or otherwise unverifiable IDs return
  `setAuto ownership could not be verified for keyword IDs: [...]`.
- After provider apply, DirectPilot reads back the same scope via
  `keywordbids.get` because `setAuto` does not return calculated bid values.
  Keyword readback enforces the same complete, exactly-once ownership check even
  without `LimitedBy`. A failed or unverifiable keyword readback, or a truncated
  broad (`campaign`/`ad_group`) readback, returns `applied=false`,
  `partial_failure=true`, `readback_failed=true`, and a concrete sanitized
  verification error; do not claim rollback or fabricate bid values.

### Existing and new bid modifiers (demographic, weather, etc.)

- Endpoints:
  - `GET /yandex/campaigns/{campaign_id}/bid-modifiers` reads all existing modifier items Direct returns for the campaign, including weather modifiers when present.
  - `POST /yandex/campaigns/{campaign_id}/bid-modifiers` previews/applies changes to existing modifier coefficients (`bidmodifiers.set`).
  - `POST /yandex/campaigns/{campaign_id}/bid-modifiers/create` previews/applies creating new modifier rows (`bidmodifiers.add`).
- Source of record is read-first + write:
  - read current rows and `modifier_id` first;
  - updates: `POST .../bid-modifiers` with `dry_run=true` for preview;
  - updates: `POST .../bid-modifiers` with `dry_run=false` only after explicit user approval;
  - create: `POST .../bid-modifiers/create` with `dry_run=true` first, then `dry_run=false` after approval.
- Required live apply gates for both set/create: `DIRECTPILOT_MODE=live_write`, `approved=true`,
  `idempotency_key`, `dry_run=false`.
- Contract:
  - existing update requires `modifier_id` and applies by `Id + BidModifier` only;
  - `adjustment_percent=-100` maps to `BidModifier=0` (or pass direct `bid_modifier`);
  - create payloads use `bidmodifiers.add` (`CampaignId`/`AdGroupId` + family block), not `modifier_id`;
  - documented add families include devices (`MOBILE`, `TABLET`, `DESKTOP`, `DESKTOP_ONLY`, `SMART_TV`), demographics, retargeting, regions, video/format, smart ads, SERP layout, income grade and ad-group coefficient;
  - official create enum values are enforced (`IOS/ANDROID`, `GENDER_MALE/GENDER_FEMALE`, `AGE_*`, `ALONE/SUGGEST`, `VERY_HIGH/HIGH/ABOVE_AVERAGE`);
  - v5 plural array blocks are required for `DemographicsAdjustments`, `RetargetingAdjustments`, `RegionalAdjustments`, `SerpLayoutAdjustments`, `IncomeGradeAdjustments`;
  - `type_hint`, `age_range`, and `conditions` are preview/readability metadata for both flows and must not be sent in `bidmodifiers.set` payloads.
- Store behavior:
  - `bidmodifiers.add`/`set` use provider `AddResults`/`SetResults`; `AddResults[].Ids` (plural) is the documented bidmodifiers.add envelope; any item errors return `partial_failure=true`, `applied=false` while keeping idempotent replay consistent.
  - successful writes do best-effort `bidmodifiers.get` readback where possible; Direct `bidmodifiers.get` requires `SelectionCriteria.Levels=["CAMPAIGN","AD_GROUP"]`, otherwise sandbox/live returns `error_code=8000` / missing `Levels`.
- Error behavior:
  - missing `modifier_id` on update apply -> HTTP 409 before network write;
  - provider/unexpected failures -> HTTP 502 with redacted diagnostics and audit event `yandex_bid_modifiers_failed`;
  - weather create is unsupported and rejected before provider calls: live Direct returned `error_code=8000` unknown parameter `WeatherAdjustment`; do not try guessed alternative shapes. Existing weather update remains Id-based via `bidmodifiers.set`; `humidity`, `wind`, and spend-category/«Уровень трат в категории» are intentionally unsupported until verified mapping exists.
  - Optional sandbox smoke for existing modifier updates is in `tests/test_yandex_bidmodifiers_sandbox_smoke.py`; it is skipped unless `DIRECTPILOT_YANDEX_SANDBOX_SMOKE=bidmodifiers_set` and `YANDEX_DIRECT_SANDBOX_TOKEN` are set. It can use explicit `YANDEX_DIRECT_SANDBOX_MODIFIER_ID`, or discover an existing modifier via `YANDEX_DIRECT_SANDBOX_CAMPAIGN_ID` plus optional `YANDEX_DIRECT_SANDBOX_MODIFIER_TYPE`, then performs a no-op `bidmodifiers.set` with the current `BidModifier` and readback. Do not auto-create weather modifiers: Direct v5 rejected `WeatherAdjustment` create.
- After live apply, endpoint returns changed rows from `bidmodifiers.get` readback.

### Autotargeting settings

### Autotargeting settings (mandatory for search ad groups)

- Search / Search+YAN `TEXT_AD_GROUPs` require autotargeting (`---autotargeting` keyword row). Deleting or fully disabling it can be invalid — configure categories and brand options instead.
- Use `GET /yandex/campaigns/{campaign_id}/autotargeting` to read current settings (per ad group: categories, brand options, status).
- Use `POST /yandex/campaigns/{campaign_id}/autotargeting` to update settings (standard gate: dry_run default, live_write for apply).
- **Default preset for local service-search campaigns:** `exact_narrow` (Exact=YES, Narrow=YES, Alternative=NO, Accessory=NO, Broader=NO).
- **Default brand options:** WithoutBrands=YES, WithAdvertiserBrand=YES, WithCompetitorsBrand=NO.
- **Do not enable all autotargeting categories by default.** Agents must explicitly ask the user or select the `exact_narrow` preset before committing.
- `Broader=YES` is optional only by explicit reach trade-off. Alternative and Accessory should not be enabled by default.
- The endpoint uses `AutotargetingSettings` (with `Categories` + `BrandOptions`), NOT the deprecated `AutotargetingCategories`.
- In `keywords.add`, categories not explicitly YES/NO are treated as enabled by Direct API. DirectPilot always sends all five category booleans + all three brand booleans explicitly.
- Endpoint does read-before-write: `keywords.get` → find `---autotargeting` rows → build `keywords.update` by keyword `Id`.
- If an ad group lacks an autotargeting row, the endpoint skips it (reports in `skipped_ad_group_ids`) when `create_missing=False`. Set `create_missing=True` to create new `---autotargeting` rows via `keywords.add` (gated: requires dry-run preview, then `live_write` + `approved` + `idempotency_key`).
- See `docs/API_SIMPLE.md` section 13 and `docs/MARKETER_GUIDE.md` for full marketing guidance.

### UTM operations

UTM workflow for Yandex Direct campaigns:

**Existing campaigns — audit → plan → user approval → apply → readback:**

1. **Audit first:** `GET /yandex/campaigns/{campaign_id}/utm-audit` — read-only inventory of UTM status per ad/sitelink.
2. **Plan/preview:** `POST /yandex/campaigns/{campaign_id}/utm-plan` — always dry_run, shows old_url→new_url. Never writes.
3. **User approval (HUMAN APPROVAL CONTRACT):** show the concrete diff to the user. Ask about:
   - `campaign_slug` — if ambiguous, generate from campaign name+id (safe default).
   - `overwrite` — whether to replace existing UTM (default: false, preserve existing).
   - sitelinks — include in preview/apply only when requested; preserve existing titles/descriptions and set structure.
   Do NOT apply without explicit user confirmation. `approved=true` is a technical
   flag, not agent self-approval.
4. **Apply:** `POST /yandex/campaigns/{campaign_id}/utm-apply` with `approved=true`, `idempotency_key`, `DIRECTPILOT_MODE=live_write`, `dry_run=false`.
5. **Readback:** response includes `readback` for ads and `sitelink_readback` for sitelinks with confirmed new URLs after successful apply.

**New campaigns (live-create) — UTM at birth:**

- Include `utm_config` in `POST /yandex/campaigns/live-create` so ads are born with UTM.
- `LiveCreateCampaignRequest.utm_config` **overrides** `draft.utm_config` when both are set:
  - If passed — the request-level config takes precedence (draft is not mutated).
  - If omitted — `draft.utm_config` is the fallback.
  - `enabled=false` in the override suppresses UTM even if the draft has it enabled.
- This allows UTM to be set at creation time without editing the draft, and allows
  per-creation slug/overwrite choices.

**When to ask the user:**
- Campaign slug/naming is unclear.
- Overwrite of existing UTM is requested.
- Sitelinks UTM apply is desired; include `include_sitelinks=true` and verify `sitelink_readback` after apply.

Default UTM convention:
- `utm_source=yandex`, `utm_medium=cpc` (fixed).
- `utm_campaign=<slug>` (from campaign name+id, or operator-provided).
- `utm_content=<ad_id>` (auto-filled from ad data).
- `utm_term` left empty (keyword-level mapping not yet implemented).

Safety:
- `ads.update` is REPLACE-shaped: Title, Text, Href MUST be re-sent.
- `BusinessId`, `SitelinkSetId`, `VCardId`, `Title2` are preserved from readback.
- `live_readonly` blocks writes with HTTP 409 before any network call.
- Custom params supported but never override core five UTM params.
- Sitelink apply uses `sitelinks.update` through DirectPilot gates; fail closed on provider errors and do not report success without readback.

### Existing-campaign landing URL migration

Use the dedicated DirectPilot routes; do not issue raw Direct writes:

- `POST /yandex/campaigns/{campaign_id}/ads/landing-urls` — migrate existing `TEXT_AD` hrefs using per-ad `expected_href` and `target_href`.
- `POST /yandex/campaigns/{campaign_id}/sitelinks/migrate-urls` — migrate one source sitelink set using exact `expected_items` and `target_items`.
- `POST /yandex/campaigns/{campaign_id}/landing-url-migrations` — combined ad + sitelink migration. It is non-transactional.

Required sequence:
1. Read current state and send `dry_run=true` (the default). No provider write is allowed in preview.
2. Inspect `changes`, `payload_preview`, and the account-wide `reference_scan`; show the exact diff to the user.
3. Obtain explicit user approval for that diff.
4. Operator applies only with `DIRECTPILOT_MODE=live_write`, `approved=true`, `dry_run=false`, and a non-empty `idempotency_key` (minimum 6 characters).
5. Require successful per-item `provider_results` and `readback`; treat `partial_failure=true` or `completed=false` as not applied.

Safety invariants:
- `expected_href`/`expected_items` are optimistic-concurrency snapshots; mismatch fails closed before provider writes.
- Only route-owned `TEXT_AD` rows are updated.
- Every target must be HTTPS, use a host in `DIRECTPILOT_URL_MIGRATION_ALLOWED_HOSTS`, resolve only to public addresses, survive bounded same-host redirects, return 2xx, and contain a requested fragment anchor. An empty allowlist fails closed.
- Sitelinks are cloned with documented `sitelinks.add`, verified, then reattached with `ads.update`. Never use `sitelinks.update` or delete the source set in this workflow.
- The full account is scanned fail-closed: `campaigns.get` supplies the inventory, paginated `ads.get` uses supported `SelectionCriteria.CampaignIds`, and `TextAd.SitelinkSetId` is filtered locally. Never use `SelectionCriteria.SitelinkSetIds`. Ignore unrelated/no-sitelink ads; only references in the route campaign are reattached, while other campaigns stay on the source set.
- Limited or malformed campaign inventory, invalid/duplicate campaign IDs, provider errors, or malformed/duplicate matching ad IDs block the operation before writes.
- Target fetch timeouts/transport failures return a controlled HTTP 409. Provider diagnostics expose only available allowlisted fields (`provider`, `service`, `method`, `operation`, `http_status`, `error_code`, `error_string`, `error_detail`), never OAuth, tokens, request bodies, or `units`.
- Provider item errors or readback mismatch produce `partial_failure`; do a new read/preflight and never blind-retry or blind-rollback.
- URL-migration idempotency is currently process-local and does not survive a restart.

## Verification checklist

- [ ] Tests pass.
- [ ] `live_readonly` still blocks writes.
- [ ] `dry_run=true` remains non-mutating.
- [ ] OpenAPI is synchronized if routes changed.
- [ ] No secrets or private customer data were committed.
