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

1. Read `README.md`, `CONTRIBUTING.md`, and `docs/SAFETY_MODEL.md`.
2. Identify whether the change is docs-only, read-only behavior, or write-path behavior.
3. For docs-only changes, keep endpoint descriptions synchronized with OpenAPI.
4. For code changes, add or update tests before claiming completion.
5. Run:
   ```bash
   uv run pytest -q
   uv run python scripts/check_openapi_sync.py
   uv run python scripts/check_no_secrets_in_git.py
   ```
6. Include the executed commands and results in the PR.

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

## Verification checklist

- [ ] Tests pass.
- [ ] `live_readonly` still blocks writes.
- [ ] `dry_run=true` remains non-mutating.
- [ ] OpenAPI is synchronized if routes changed.
- [ ] No secrets or private customer data were committed.
