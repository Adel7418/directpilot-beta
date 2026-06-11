# Yandex Business contact binding runbook

## Purpose

Attach a published Yandex Business organization to text ads after `POST /yandex/vcards` is blocked by `error_code=3500` (for example: `Создание визиток не поддерживается`) by using `POST /yandex/ads/business`.

## Scope

- Read-only check is always possible in any live mode.
- Real write is allowed only with `DIRECTPILOT_MODE=live_write`.
- No secrets are included in payloads or examples.

## Endpoint quick map

- `POST /yandex/ads/business` — attach `BusinessId` to one or more ads through `ads.update`.
- `GET /yandex/businesses` — discover/verify business metadata.
- `GET /yandex/campaigns/{campaign_id}/ads` — verify attachment results.

## Preconditions

1. Verify that `POST /yandex/vcards` returned:
   - `error_code=3500`
2. Have the target values:
   - `business_id` to attach
   - either `campaign_id` or explicit `ad_ids`
   - expected phone number for extra verification
3. Ensure `approved=true` and choose `idempotency_key` (minimum length rule is enforced in the API contract).

## Safe workflow (concise)

### 1) Confirm organization before write

`GET /yandex/businesses`

- From the response, locate the target record by `Id` and read fields:
  - `Id`
  - `Name`
  - `Phone`
  - `ProfileUrl`
  - `IsPublished`
  - `Urls`
  - `HasOffice`
- Continue only if:
  - `IsPublished == "YES"`
  - the phone matches the expected organization phone

### 2) Dry-run attach request

`POST /yandex/ads/business`

```json
{
  "approved": true,
  "idempotency_key": "dp-biz-attach-2026-01",
  "dry_run": true,
  "business_id": 11588384335,
  "campaign_id": 710691939
}
```

Checklist after dry-run:

- response `applied == false`
- response `source == "yandex"` in live read modes
- `payload_preview` contains `method="ads.update"`
- each preview item has `TextAd.BusinessId` and `TextAd.PreferVCardOverBusiness == "NO"`
- `Title`, `Text`, `Href` are present for each target ad

### 3) Live write

`POST /yandex/ads/business`

```json
{
  "approved": true,
  "idempotency_key": "dp-biz-attach-2026-01",
  "dry_run": false,
  "business_id": 11588384335,
  "campaign_id": 710691939
}
```

Requirements:

- `DIRECTPILOT_MODE=live_write`
- same or intentionally renewed `idempotency_key`
- `approved=true`
- `dry_run=false`

Successful response should include:
- `applied == true`
- `dry_run == false`
- `ad_ids` list of target text ads
- `skipped` list (if non-text ads were ignored)
- no secret values

Live example confirmation:

- `business_id=11588384335`
- `campaign_id=710691939`
- `ad_ids=17747346245..17747346249`
- expected: `BusinessId=11588384335`, `PreferVCardOverBusiness="NO"`, `VCardId=null`

### 4) Readback verification

`GET /yandex/campaigns/{campaign_id}/ads` (or equivalent readback path that exposes `TextAd` fields).

Validate for each target ad:

- `TextAd.BusinessId` equals requested `business_id`
- `TextAd.PreferVCardOverBusiness == "NO"`
- `TextAd.VCardId` remains `null` when using Business-only binding

## Notes

- Endpoint behavior with `campaign_id`:
  - reads campaign ads first,
  - applies only to `TEXT_AD`,
  - non-text ads are returned in `skipped` with reason `not_text_ad`.
- For non-text ad targeting use explicit `ad_ids` and verify each id manually.

## Execution checklist

- [ ] `POST /yandex/vcards` blocked with `error_code=3500`
- [ ] Organization verified by `GET /yandex/businesses`
- [ ] `IsPublished` and phone check passed
- [ ] dry-run payload inspected and approved
- [ ] live call executed only in `live_write`
- [ ] readback confirmed for `BusinessId`, `PreferVCardOverBusiness`, `VCardId`

