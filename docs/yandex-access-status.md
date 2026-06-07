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
keywordsresearch.createNewWordstatReport
keywordsresearch.getWordstatReport
keywordsresearch.deleteWordstatReport
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
