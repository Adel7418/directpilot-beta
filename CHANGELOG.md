# Changelog

All notable changes to DirectPilot will be documented in this file.

The project follows semantic versioning during beta using `0.x.y` versions. Breaking changes may happen before `1.0.0`, but they should still be documented.

## [0.2.1] - 2026-06-11

### Fixed

- Documented the correct Yandex Direct lifecycle for newly created DRAFT campaigns: do **not** launch DRAFT campaigns with `campaigns.resume`; send draft ads to moderation with `ads.moderate` (`SelectionCriteria.Ids=[ad_ids]`).
- Added the observed launch result for campaign `710691939`: `campaigns.resume` returned per-item `Code=8300` because the campaign was still a draft; `ads.moderate` for ads `17747346245..17747346249` returned empty per-ad errors and readback showed campaign `Status=MODERATION`, `State=ON`, ads `Status=MODERATION`, `State=OFF` pending moderation.
- Clarified that `campaigns.resume` remains only for already-created stopped/suspended campaigns, while DRAFT-to-moderation is a separate Direct lifecycle step.

## [0.2.0] - 2026-06-11

### Added

- Public beta repository governance files.
- Contribution, security, and safety-model documentation.
- GitHub issue and pull request templates.
- CI and security workflow definitions.
- Sanitized DirectPilot operations skill for agent-assisted workflows.
- `POST /yandex/campaigns/live-create` now executes the full staged write
  chain for draft publish:
  `campaigns.add` → `adgroups.add` → `ads.add` → `keywords.add`.
  - Works in `dry_run` for stage preview in all modes.
  - Real apply requires `DIRECTPILOT_MODE=live_write`, `approved=true`,
    `idempotency_key`; `live_readonly`/`mock`/`sandbox` reject write
    before network.
  - Stage failure is fail-closed: any `AddResults.Errors` or missing stage
    `Id` aborts the chain, records `live_create_campaign_failed`, and no
    later stage is sent.
  - Response now includes `campaign_id`, `ad_group_ids`, `ad_ids`,
    `keyword_ids`, `stages_executed`, `not_implemented`.
  - `idempotency_key` scope is now `idempotency_key + dry_run` (separate
    cache lines for preview vs apply).
  - `negativekeywordsharedsets.add` remains intentionally not implemented;
    group-level negatives are sent via `adgroups.add` `NegativeKeywords.Items`.
  - `campaigns.add` does not force lifecycle status; DRAFT-to-moderation is
    handled outside the live-create chain. See `0.2.1` for the corrected
    lifecycle note: use `ads.moderate` for newly created DRAFT ads, not
    `campaigns.resume`.
  - `adgroups.add` items now always carry `RegionIds` (v5 rejects items
    without a geo target). The ids are resolved from `draft.region` via
    the explicit local map `_REGION_NAME_TO_V5_IDS` in `app/store.py`
    (helper `_resolve_region_to_ids`). No external lookup, no network
    call. Supported region names in the Beta: `Казань` → `[43]`,
    `Москва` → `[213]`, `Санкт-Петербург` / `СПб` → `[2]`,
    `Россия` / `Russia` → `[225]`. Trivially extensible — add one
    entry, no other change required.
  - An unmapped / empty / whitespace `draft.region` fails closed BEFORE
    any `campaigns.add` network call. The chain raises
    `YandexDirectError` with a redacted message that names the offending
    region, the public store method audits `live_create_campaign_failed`
    (no token in the audit), and the endpoint returns HTTP 502. The
    dry-run preview surfaces the same failure so the operator sees the
    same mode in both paths.
  - The `adgroups.add` payload does NOT carry a `Status` field on the
    v5 items — lifecycle/moderation state is controlled by Direct, not
    by the create chain.
  - The `NegativeKeywords` block on `adgroups.add` is OPTIONAL on v5:
    when the draft has no negatives the block is omitted entirely;
    when it has items the block is included with the items. Empty
    `Items` lists are NOT sent (v5 rejects them on some edge cases).
  - Final operational launch/ moderation checks remain outside the chain;
    the live-create path does not auto-activate campaigns.

### Fixed

- `POST /semantic-changes/{package_id}/apply` returned an opaque
  FastAPI 500 with no audit event when a non-typed exception escaped
  the store apply loop (e.g. a `RuntimeError` from a custom httpx
  transport, a `TypeError` from a malformed operation payload, or a
  `ValueError` from a non-numeric `Units` header). The apply path now
  normalises any non-typed exception into a `YandexDirectError`,
  records a `semantic_change_apply_failed` audit event with the
  exception type, and re-raises so the endpoint returns 502 with a
  redacted message. The same safety net is also added at the
  endpoint layer as a last-resort guard. The `Units` response value
  is now coerced via a defensive `_safe_units` helper that never
  raises on non-integer-looking strings. The same hardening is
  applied to the new `live_create_campaign` store method.

### Safety

- Documented `live_readonly` as the default beta mode.
- Documented write gates: `live_write`, `approved=true`, `idempotency_key`, and dry-run behavior.
