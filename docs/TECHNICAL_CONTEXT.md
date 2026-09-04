# DirectPilot Beta — Technical Context

## Purpose of this document

This file holds technical details that do not belong in the root README:

- operational boundaries;
- runtime modes and their write behavior;
- write-control gates;
- retired/legacy routes;
- source-of-truth location of API contracts and operational status.

For API usage, start with:

- `docs/API_SIMPLE.md`
- `docs/implementation_scope.md`
- `docs/openapi.json`

## Boundary and ownership

DirectPilot owns API surfaces for:

- Yandex Direct integration;
- Yandex Metrika integration;
- Yandex Search API v2 Wordstat integration;
- application-level permissions, audit records, and safety controls.

DirectPilot is not a UI product and is not part of Hermes runtime behavior.

## Runtime mode model

### Current operating model

- **Product orientation:** `live-first`
- **Active mode in practice:** `live_readonly`

### Mode behavior

| Mode | Purpose | External write behavior |
|---|---|---|
| `live_readonly` | Production-first read path | Writes blocked by default; safe reads from production continue |
| `live_write` | Explicit controlled write mode | Allowed only when write gates are satisfied |
| `sandbox` / `mock` | Legacy/dev fallback only | Not product path; available only for compatibility/diagnostics |

Retired UI/demo routes are not part of product API and should not be used for business logic:
`/`, `/demo/yandex-status`, `/demo/campaigns`, `/demo/report`, `/demo/recommendations`, `/demo/tools`, `/demo/security-approval`.

## Yandex access status (summary)

Source of truth: `docs/yandex-access-status.md`.

- Direct API production read is active through `https://api.direct.yandex.com/json/v5`.
- `DIRECTPILOT_MODE=live_readonly` is the active environment assumption for verified local checks.
- Key read endpoints are verified through the status checks in `yandex-access-status.md` (campaigns, ad-groups, ads, keywords, reports, status).
- Legacy write methods are intentionally limited; full `suspend`/`resume` support is intentionally controlled.
- OAuth tokens are loaded from local configuration and must not be logged or written to documentation.

## Live-write control gates

Writes are enabled only in explicit controlled mode. Implementation contract:

- `DIRECTPILOT_MODE=live_write`
- `approved=true`
- `dry_run=false`
- request-scope `idempotency_key`
- required runtime checks/audit entry before send

In non-write mode, supported write-style endpoints must return safe dry-run behavior (`applied=false` equivalent) and perform no production mutation.

### What `live_write` means, in plain language

`live_readonly` and `live_write` are the two **runtime modes** DirectPilot
runs in. They do NOT refer to specific endpoints — the same endpoint can
behave differently depending on which mode is active.

- **`live_readonly`** (the default in production): DirectPilot is allowed
  to *read* real Yandex Direct data, but every write-style request
  (pause / resume, vCard add, semantic-change apply, ...) is rejected
  before any network call to Yandex. The only safe exception is
  `dry_run=true` — that returns a preview of the request that *would*
  be sent.
- **`live_write`**: DirectPilot is allowed to *write* to Yandex Direct,
  but only when **all three** of the following gates are satisfied at
  the same time:
  1. `approved=true` on the request body — the caller explicitly
     confirms they want to apply the change.
  2. `idempotency_key` is supplied (length ≥ 6) — the same key
     returns the cached result, so retrying never double-applies.
  3. `dry_run=false` — the request is a real apply, not a preview.

  Without all three, the request is rejected with HTTP 409 *before*
  any HTTP call to Yandex Direct. The OAUTH token is never echoed
  back to the caller.

So: "live_write" is **not a verb** and **not a Yandex Direct
permission** — it is a *mode switch* on the DirectPilot service that
unlocks real writes, gated by approval + idempotency + dry_run.

## Semantic change package (staged / dry-run-first)

The `live_readonly` / `live_write` gates above apply equally to a new
staged workflow for changing the *semantics* of an existing Yandex
Direct campaign (e.g. campaign `710382063` — dishwasher repair, Kazan).
Two endpoints:

- `POST /campaigns/{campaign_id}/semantic-changes` — build a staged
  package. **Always pure-local**: no network call, no approval
  required. The response includes a `preview` listing the Direct API
  v5 `keywords.add` / `adgroups.update` operations that *would* be
  sent on apply. Use this to design and review a change before
  deciding to apply it.
- `POST /semantic-changes/{package_id}/apply` — apply a previously
  prepared package. **Live apply is `live_write`-only and stricter
  than older sandbox-enabled writes.** `dry_run=true` is allowed in
  every live mode and returns an audited preview; `dry_run=false` is
  rejected in `live_readonly` and in `sandbox` — even when all the
  generic write gates (`approved=true`, `idempotency_key`) are
  satisfied. The reason is data-loss risk on Direct's
  `NegativeKeywords.Items` REPLACE semantics: the live apply path
  performs a read-modify-write (`adgroups.get` + `adgroups.update`)
  that is too risky to exercise against a sandbox token (which has
  the same account shape as a real one). The generic write gate
  still applies, but it does not bypass the semantic-change block.

### Why semantic changes are stricter than pause/resume and vCard add

`live_readonly` blocks *all* real writes; that part is shared with
`pause`/`resume` and `vcards.add`. The **extra** restriction for
semantic changes is the `sandbox` block. Pause/resume and vCard add
both become write-eligible in `sandbox` (their operations are
idempotent and don't lose data on a partial failure). Semantic
changes cannot, because:

1. Direct API v5 `adgroups.update` with `NegativeKeywords.Items` is
   REPLACE, not APPEND. A naive send would silently drop the
   group's pre-existing negatives.
2. To avoid that, the live apply path reads the current
   group-level negatives via `adgroups.get` and merges them with
   the requested phrases (order-preserving, de-duplicated) before
   calling `adgroups.update`. If the read fails or the target ad
   group id is not in the response, the apply aborts with
   `YandexDirectError` and a `semantic_change_apply_failed` audit
   event — *before* any update is sent.
3. The sandbox token has the same account-shape as a real one, so
   "sandbox" here is misleading: a failed merge against a real
   campaign shape still loses data. We keep the gate strict until
   (a) a persistent, idempotent production store is in place so
   packages can be replayed safely and (b) an explicit sandbox test
   account is wired up. Until then, **only `live_write` against the
   user-confirmed production campaign may run a real
   semantic-change apply.**

The pure-local `prepare` path and the `dry_run=true` apply path
stay allowed in every mode (including `sandbox` and
`live_readonly`); only the real `dry_run=false` apply is gated by
`live_write`. The `preview` field on the prepare response carries
`merge_on_apply: true` and a `semantics_note` describing the
read-modify-write so the user sees the contract before they
approve.

Both endpoints record audit events (`semantic_change_prepared`,
`semantic_change_apply_dry_run`, `semantic_change_apply_failed`,
`semantic_change_applied`) and never echo the OAUTH token.

## Confirmed v5 method names

The staged workflow builds the **request envelopes** for the
Direct API v5 services that are now confirmed against the public
docs:

- `keywords.add` — service `keywords`, method `add`. See
  https://yandex.com/dev/direct/doc/ref-v5/keywords/keywords.add.html
- `adgroups.update` — service `adgroups`, method `update`. The
  `NegativeKeywords.Items` field on this method is REPLACE (not
  APPEND), which is why the live apply path performs a
  read-modify-write via `adgroups.get` first.

Both methods are used in production today; no further research is
needed before the next `DIRECTPILOT_MODE=live_write` apply call
goes out. Rate-limit / units costs are returned in the
`Units` response header and surfaced on the audit event as
`yandex_units`.

## What changed in API scope (recently reflected in docs)

`docs/implementation_scope.md` is the canonical list of currently included and excluded endpoints.

- Included read operations (production): campaigns, ad-groups, ads, keywords, finance/report endpoints.
- Included write operations (limited): controlled pause/resume-style controls.
- Explicitly excluded for now: unscoped live campaign creation/update, blind budget/setting mutation without approvals, uncontrolled write endpoints. Narrow, documented read-before-write exceptions (for example the guarded priority-goal value endpoint) remain subject to the full `live_write` gate and mandatory readback.
- Wordstat read path is split: Yandex Search API v2 in `/wordstat/*`; legacy report lifecycle from v4 is intentionally not pushed through Direct API v5 for those flows.

## OpenAPI locations

- Static contract: `docs/openapi.json`
- Runtime endpoint (when service is running): `http://127.0.0.1:8000/openapi.json`

## Source-of-truth doc map

- `docs/API_SIMPLE.md` — practical API map
- `docs/MARKETER_GUIDE.md` — marketer task-to-endpoint map
- `docs/implementation_scope.md` — what is implemented vs excluded
- `docs/yandex-direct-application-spec.md` — external app-level spec details
- `docs/yandex-access-status.md` — verification status and checks
- `docs/openapi.json` — machine-readable contract