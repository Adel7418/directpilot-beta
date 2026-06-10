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

## Idempotency

Write-style operations must use idempotency keys so repeated submissions do not duplicate campaign actions. Tests should cover replay behavior when practical.

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
