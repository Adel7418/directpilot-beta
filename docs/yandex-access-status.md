# Yandex Direct API access status — DirectPilot Beta

## Current status

Access to Yandex Direct API is approved and verified for real production read-only data.

```text
DIRECTPILOT_MODE=live_readonly
Direct API base URL: https://api.direct.yandex.com/json/v5
Login: [REDACTED]
ClientId: [REDACTED]
```

## Verified production read-only calls

Last local verification:

```text
GET /health -> mode=live_readonly
GET /integrations/yandex/direct/status -> ok, Login=[REDACTED], ClientId=[REDACTED]
GET /yandex/campaigns -> source=yandex, count=1
GET /yandex/campaigns/[REDACTED]/ad-groups -> source=yandex, count=1
GET /yandex/campaigns/[REDACTED]/ads -> source=yandex, count=1
GET /yandex/campaigns/[REDACTED]/keywords -> source=yandex, count=32
```

Representative production campaign:

```text
CampaignId: [REDACTED]
Status: ACCEPTED
Type: TEXT_CAMPAIGN
AdGroup count: 1
Ads count: 1
Keywords count: 32
```

## Implemented Yandex Direct methods

Read-only production data:

```text
clients.get
campaigns.get
campaigns.get (finance shape: Funds, Statistics, DailyBudget, StartDate, EndDate)
AccountManagement.Get (Live v4, Action=Get, read-only account balance)
adgroups.get
ads.get
keywords.get
bids.get
changes.check
changes.get
dictionaries.get
bidmodifiers.get
negativekeywordsharedsets.get
retargetinglists.get
audiencetargets.get
keywordsresearch.hasSearchVolume
keywordsresearch.deduplicate
reports: CAMPAIGN_PERFORMANCE_REPORT
reports: ADGROUP_PERFORMANCE_REPORT
reports: AD_PERFORMANCE_REPORT
reports: CRITERIA_PERFORMANCE_REPORT
reports: SEARCH_QUERY_PERFORMANCE_REPORT
sitelinks.get
vcards.get
adimages.get
creatives.get
feeds.get
businesses.get
agencyclients.get
```

## Yandex Metrika read-only

Metrika is implemented as a separate read-only integration and uses `YANDEX_METRIKA_OAUTH_TOKEN`, not the Direct OAuth token.

```text
GET /metrika/counters                              -> management/v1/counters
GET /metrika/counters/{counter_id}/goals           -> management/v1/counter/{counter_id}/goals
GET /metrika/counters/{counter_id}/summary         -> stat/v1/data with ym:s:anyGoalReaches
GET /metrika/counters/{counter_id}/traffic-sources -> stat/v1/data with ym:s:lastsignTrafficSource
```

The aggregate goal metric is `ym:s:anyGoalReaches`; `ym:s:goalReaches` is intentionally not used because it is rejected by the Metrika Stats API.

## Yandex AI Studio / Search API v2 Wordstat

Modern Wordstat access is implemented separately from Direct API v5 and uses `YANDEX_SEARCH_API_KEY`, not the Direct OAuth token.

```text
GET /wordstat/top?phrase=...&regions=43&limit=10        -> topRequests
GET /wordstat/dynamics?phrase=...&regions=43&date_from=2026-01-01T00:00:00Z -> dynamics
GET /wordstat/regions?phrase=...                        -> regions
GET /wordstat/regions-tree                              -> getRegionsTree
```

Direct API v5 `keywordsresearch` supports only `hasSearchVolume` and `deduplicate`; legacy Wordstat report lifecycle methods are not sent to v5 and return `UNSUPPORTED_IN_V5` in the v5 client. For `dynamics`, date boundaries must match the requested period (`PERIOD_MONTHLY`: first day of month; `PERIOD_WEEKLY`: Monday→Sunday).

## Yandex Search API v2 Web Search

`GET /search/web?query=...&page=0&region=225&limit=10` is a separate read-only Web Search client, not Direct API or Wordstat. It requires both `YANDEX_SEARCH_API_KEY` and the configured `YANDEX_SEARCH_FOLDER_ID`; the folder is never accepted from a caller. The API key supports both Wordstat and Web Search. Web Search consumes provider quota/billing and requires IAM role `search-api.webSearch.user` (https://github.com/yandex-cloud/docs/blob/master/en/_roles/search-api/webSearch/user.md). No automatic upstream smoke call runs at startup; credentials, Authorization, folder ID, XML and `rawData` are never returned or logged.

Limited write adapter implemented but not enabled in current mode:

```text
campaigns.suspend
campaigns.resume
```

Real write calls require:

```text
DIRECTPILOT_MODE=live_write
approved=true
dry_run=false
idempotency_key=<unique key>
```

## Safety boundary

- The current operating mode is `live_readonly`.
- Real OAuth token is configured locally but never written to docs, logs, API responses, screenshots, or wiki.
- `live_readonly` can read production Direct data and cannot apply live writes.
- `dry_run=true` returns a provider-shaped response with `applied=false` and performs no write network call.

## Verification commands

```bash
uv run pytest -q
uv run ruff check app/config.py app/main.py app/store.py app/yandex_direct.py tests/test_api.py tests/test_yandex_direct_client.py tests/test_yandex_facade.py tests/test_live_control.py tests/test_yandex_read_endpoints.py tests/test_yandex_read_live_modes.py
```

Current result:

```text
117 passed, 1 warning
All checks passed
```
