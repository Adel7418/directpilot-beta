# DirectPilot Safety Model

DirectPilot is an API-first beta service for Yandex Direct, Yandex Metrika, and Wordstat. Its safety model is intentionally conservative because unsafe writes can affect advertising campaigns and budgets.

## Runtime modes

| Mode | Intended use | External reads | External writes |
|---|---|---:|---:|
| `mock` | local development fallback | no | no |
| `sandbox` | legacy/dev compatibility only | limited | not a product write path |
| `live_readonly` | default beta mode | yes | no |
| `live_write` | explicit operator-controlled writes | yes | yes, gated |

`live_readonly` is the public beta default. Any write-capable endpoint must fail closed in `live_readonly` before making a mutation request.

## Write-gate contract

A real write is allowed only when all conditions are true:

1. runtime mode is `live_write`;
2. request has `approved=true`;
3. request has a valid `idempotency_key`;
4. request has `dry_run=false`;
5. endpoint-specific validation passes;
6. the Yandex client helper used is explicit and test-covered.

`dry_run=true` is always preview-only and must return `applied=false`.

**Technical `approved=true` is NOT a self-approval by an agent or script.**
The field is a protocol gate; the decision to set it must come from a human
operator who has seen the concrete dry-run preview / diff / impact summary and
explicitly confirmed the action in chat, ticket, or operator UI.  An agent
(Marketer, coder, reviewer, or automated script) must never set `approved=true`
on its own initiative — it prepares recommendations and previews; the operator
applies after user approval.

## Human Approval Contract

All Direct/Metrika write operations (minuses, keywords, ads, moderation, strategy, bids,
budgets, time-targeting, autotargeting, goals, UTM, pause/resume, live-create)
require this sequence before `approved=true`:

1. **Preview** — read-only endpoints + `dry_run=true` to produce a concrete
   diff of what will change (which entities, expected effect, risk).
2. **Show** — present the diff to the user in human-readable form.
3. **Confirm** — the user explicitly approves the exact action.  Silence,
   an unrelated "ok", or a general instruction is NOT approval.
4. **Apply** — only then send `approved=true`, `idempotency_key`,
   `dry_run=false`, with `DIRECTPILOT_MODE=live_write`.
5. **Readback** — verify what changed via response readback or dedicated follow-up read endpoint.

Marketers prepare recommendations; operators/orchestrators apply after
confirmation.  No agent self-approves.

## Idempotency

Write-style operations must use idempotency keys so repeated submissions do not duplicate campaign actions. Tests should cover replay behavior when practical.

URL-migration idempotency is currently process-local. After a process restart, treat a repeat as a new operation: run a fresh preflight and obtain fresh explicit approval before applying it.

## Audit and error handling

Audit logs should record enough context to understand the action without exposing secrets. Error responses must not include OAuth tokens, client secrets, API keys, raw authorization headers, or private account exports.

## Contributor rules

A pull request is high-risk if it changes:

- runtime mode interpretation;
- write gates;
- Yandex API client behavior;
- semantic-change/vCard/keyword/campaign write operations;
- budget-affecting behavior;
- audit logs;
- secret handling;
- CI/security workflow behavior;
- skills or agent instructions that can trigger operations.

High-risk PRs require maintainer review and concrete test evidence.

## Verification checklist

Before merging safety-sensitive changes, verify:

- [ ] `uv run pytest -q` passes.
- [ ] `live_readonly` blocks real writes before mutation network calls.
- [ ] `dry_run=true` does not mutate external state.
- [ ] no secrets appear in tests, logs, docs, or error responses.
- [ ] OpenAPI docs are synchronized if routes changed.
