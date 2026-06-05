# Yandex access status

Last checked: 2026-06-05

## Credentials

Local credentials are stored only in `.env` with `chmod 600`; `.env` is ignored by git.

## Checks

Command:

```bash
uv run python scripts/verify_yandex_token.py
```

Local API smoke:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8010
# /health -> 200, mode=sandbox
# /integrations/yandex/direct/status -> 200 with structured Yandex error 58 until app registration is completed
# /openapi.json -> 200, 10 paths
```

Result:

- Yandex OAuth identity check: PASS (`login_info_status 200`).
- DirectPilot mode: `sandbox`.
- Direct API base URL: `https://api-sandbox.direct.yandex.com/json/v5`.
- Sandbox `clients.get`: BLOCKED by Yandex error `58` / `Незавершенная регистрация`.

Yandex response says the application access request must be completed in the Direct interface and approved before Direct API calls are allowed.

## Interpretation

The OAuth token is valid, and DirectPilot now targets the sandbox endpoint. However, Direct API access is still not usable from code until Yandex completes/accepts the application registration for Direct API access.

## Next action

Use the current DirectPilot Beta app/OpenAPI baseline as the demonstrable application for the Yandex Direct API access request. After approval, rerun the verification command and then enable read-only sandbox sync.
