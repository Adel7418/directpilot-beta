# Security Policy

DirectPilot integrates with advertising and analytics APIs. Treat all credentials, account identifiers, campaign data, and budget-changing operations as sensitive.

## Supported status

DirectPilot is currently public beta. Security fixes are prioritized over feature work, but APIs and internal contracts may still change.

## Reporting a vulnerability

Please do not open public issues with secrets, exploit details, tokens, screenshots containing credentials, or private customer/account data.

Use a private maintainer contact or GitHub private vulnerability reporting if enabled. Include:

- affected version/commit;
- vulnerable endpoint or file;
- reproduction steps using redacted placeholders;
- expected vs actual behavior;
- whether live write/budget/campaign state can be affected.

## Secret handling

Never commit or paste:

- `YANDEX_OAUTH_TOKEN`
- `YANDEX_CLIENT_SECRET`
- `YANDEX_SEARCH_API_KEY`
- `YANDEX_METRIKA_OAUTH_TOKEN`
- real OAuth authorization codes
- raw `.env` files
- private customer/campaign exports

If a secret is exposed, rotate it immediately in Yandex and remove it from git history before publishing.

## Live-write safety

Security-sensitive write gates:

- default mode is `live_readonly`;
- live writes require `DIRECTPILOT_MODE=live_write`;
- write requests require `approved=true`;
- write requests require an `idempotency_key`;
- `dry_run=true` must not mutate external state;
- readonly rejection must happen before external mutation calls.

Any PR that changes these gates requires maintainer review and test evidence.

## Dependency and supply-chain policy

Dependencies should be minimal. New dependencies must have a clear reason and should be compatible with server-side FastAPI usage. CI runs tests and security checks on pull requests.
