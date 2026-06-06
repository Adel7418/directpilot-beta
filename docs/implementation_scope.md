# DirectPilot Beta — Scope for campaign-management API expansion

## Goal

Turn the current mock/API-first MVP into a practical campaign constructor that agents can operate through stable REST endpoints.

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

Safe endpoints that expose current/mock external data shape without live writes:

- `GET /yandex/campaigns`
- `GET /yandex/campaigns/{campaign_id}/ad-groups`
- `GET /yandex/campaigns/{campaign_id}/ads`
- `GET /yandex/campaigns/{campaign_id}/keywords`
- `GET /yandex/reports/summary`
- `GET /yandex/reports/search-queries`

### Limited live-control facade

Only pause/resume-style actions are allowed in this scope. They must remain safe in mock/sandbox mode and must require approval/idempotency/audit gates before any real integration is later connected.

- `POST /yandex/campaigns/{campaign_id}/pause`
- `POST /yandex/campaigns/{campaign_id}/resume`

## Explicitly excluded now

- Creating live campaigns in Yandex Direct.
- Updating live campaign settings in Yandex Direct.
- Applying generated campaign payloads to Yandex Direct.
- Any endpoint that spends budget or performs write operations without explicit approval, idempotency, and audit.

## Safety rules

- Default mode is mock/sandbox.
- No secret values in responses, docs, tests, or logs.
- Write-like operations record audit events.
- Dangerous operations expose `dry_run`, `approved`, `idempotency_key`, `risk_level` or equivalent where applicable.
- Documentation must be simple enough for future agents to choose the correct endpoint without guessing.
- Some MVP `DELETE` endpoints accept JSON bodies for agent ergonomics; revisit this before putting the app behind gateways/proxies that may strip DELETE bodies.
