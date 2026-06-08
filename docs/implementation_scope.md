# DirectPilot Beta — Scope for campaign-management API expansion

## Goal

Run DirectPilot Beta as a live-first API-first campaign-control layer with active testing in `live_readonly` and explicit controlled writes via `live_write`.
Current development phase is live-only orientation: production data is primary, mock/demo are legacy/dev fallbacks only and must not drive product workflows.

## Included now

### Campaign draft constructor

- `PATCH /campaign-drafts/{draft_id}` — update draft base settings: name/business type/region/budget/landing URL.
- `POST /campaign-drafts/{draft_id}/keywords` — append keyword phrases.
- `DELETE /campaign-drafts/{draft_id}/keywords` — remove keyword phrases.
- `PATCH /campaign-drafts/{draft_id}/negative-keywords` — replace negative keywords.
- `POST /campaign-drafts/{draft_id}/ad-groups` — create ad group.
- `PATCH /campaign-drafts/{draft_id}/ad-groups/{group_id}` — update ad group.
- `DELETE /campaign-drafts/{draft_id}/ad-groups/{group_id}` — delete ad group.
- `POST /campaign-drafts/{draft_id}/ads` — create ad.
- `PATCH /campaign-drafts/{draft_id}/ads/{ad_id}` — update ad.
- `DELETE /campaign-drafts/{draft_id}/ads/{ad_id}` — delete ad.
- `POST /campaign-drafts/{draft_id}/generate-structure` — generate practical draft structure from topic/region.
- `POST /campaign-drafts/{draft_id}/validate` — validate draft before external apply.
- `GET /campaign-drafts/{draft_id}/preview` — show final payload that would be sent to Yandex Direct.
- `PATCH /campaign-drafts/{draft_id}/budget` — update daily/monthly budget and strategy.
- `PATCH /campaign-drafts/{draft_id}/bids` — update max CPC and optional per-keyword bids.

### Yandex Direct read-only facade

Safe endpoints that expose real production Yandex Direct data in `live_readonly` without live writes:

- `GET /yandex/campaigns`
- `GET /yandex/campaigns/{campaign_id}/ad-groups`
- `GET /yandex/campaigns/{campaign_id}/ads`
- `GET /yandex/campaigns/{campaign_id}/keywords`
- `GET /yandex/reports/summary`
- `GET /yandex/reports/search-queries`

### Limited live-control facade

Only pause/resume-style actions are allowed in this scope. They support dry-run in `live_readonly`; real Direct writes require `live_write`, approval, idempotency and audit gates.

- `POST /yandex/campaigns/{campaign_id}/pause`
- `POST /yandex/campaigns/{campaign_id}/resume`

## Explicitly excluded now

- Creating live campaigns in Yandex Direct.
- Updating live campaign settings in Yandex Direct.
- Applying generated campaign payloads to Yandex Direct.
- Any endpoint that spends budget or performs write operations without explicit approval, idempotency, and audit.
- Retired demo/UI paths (`/`, `/demo/yandex-status`, `/demo/campaigns`, `/demo/report`, `/demo/recommendations`, `/demo/tools`, `/demo/security-approval`) are not product API endpoints. They are excluded from OpenAPI and kept only as explicit non-product guard handlers returning 404; regression test: `tests/test_demo_ui.py`.

## Safety rules

- Current production-data mode is `live_readonly` (active testing).
- `mock` and `sandbox` are legacy/dev fallback modes only (not product path).
- No secret values in responses, docs, tests, or logs.
- Write-like operations record audit events.
- Dangerous operations expose `dry_run`, `approved`, `idempotency_key`, `risk_level` or equivalent where applicable.
- Documentation must be simple enough for future agents to choose the correct endpoint without guessing.
- Some MVP `DELETE` endpoints accept JSON bodies for agent ergonomics; revisit this before putting the app behind gateways/proxies that may strip DELETE bodies.


## Расширенный Direct API слой

Добавлен read-only/API-first слой для полного практического покрытия Яндекс Директа: reports, bids, changes, dictionaries, bid modifiers, negativekeywordsharedsets, retargeting/audience targets, `keywordsresearch.hasSearchVolume`, `keywordsresearch.deduplicate`, sitelinks, vcards, images, creatives, feeds, businesses и agency clients. Для визиток добавлен `POST /yandex/vcards` → `vcards.add`: по умолчанию dry-run; реальная запись только через `live_write`, `approved=true`, `dry_run=false` и idempotency key. Общая live-write граница не изменилась: production-записи возможны только через явное подтверждение и аудит.

## Yandex AI Studio / Search API v2 Wordstat

Добавлен отдельный read-only слой современного Wordstat API: `/wordstat/top`, `/wordstat/dynamics`, `/wordstat/regions`, `/wordstat/regions-tree`. Он использует `YANDEX_SEARCH_API_KEY` и не зависит от Direct OAuth token. Legacy Wordstat v4 report lifecycle остаётся внешним fallback-путём; Direct API v5 `keywordsresearch` не используется для create/get/delete Wordstat reports.

## Direct Live v4 баланс и Metrika read-only

Добавлены постоянные API-функции вместо одноразовых скриптов:

- `/yandex/account/balance` читает баланс общего счёта через Live v4 `AccountManagement.Get` (`Action=Get` only) и не возвращает OAuth token;
- `/yandex/campaigns/finance` читает `campaigns.get` с `Funds`, `Statistics`, `DailyBudget`, `StartDate`, `EndDate`, нормализуя micro-units в рубли;
- `/metrika/counters`, `/metrika/counters/{counter_id}/goals`, `/metrika/counters/{counter_id}/summary`, `/metrika/counters/{counter_id}/traffic-sources` читают Метрику через отдельный `YANDEX_METRIKA_OAUTH_TOKEN`;
- для агрегированных целей Метрики используется `ym:s:anyGoalReaches`, для источников — `ym:s:lastsignTrafficSource`.
