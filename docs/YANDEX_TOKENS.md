# Yandex tokens and credentials

This guide explains how to prepare local credentials for DirectPilot without committing secrets.

DirectPilot reads credentials from a local `.env` file. Start from:

```bash
cp .env.example .env
```

Never commit `.env`, real OAuth tokens, authorization codes, API keys, cookies, or raw Authorization headers.

## What DirectPilot needs

| Variable | Used for | Notes |
|---|---|---|
| `YANDEX_CLIENT_ID` | Yandex OAuth application | Public app identifier. |
| `YANDEX_CLIENT_SECRET` | Yandex OAuth application | Secret; keep local only. |
| `YANDEX_OAUTH_TOKEN` | Yandex Direct API v5 and Direct Live v4 | Direct advertising account token. |
| `YANDEX_REDIRECT_URI` | OAuth redirect | Default: `https://oauth.yandex.ru/verification_code`. |
| `YANDEX_SEARCH_API_KEY` | Yandex Search API v2 Wordstat and Web Search | API key, not a Direct OAuth token. |
| `YANDEX_SEARCH_FOLDER_ID` | Search API folder | Required operational prerequisite for `/search/web`; Wordstat may omit it. |
| `YANDEX_METRIKA_OAUTH_TOKEN` | Yandex Metrika read-only endpoints | Separate token from Direct OAuth. |

## Official docs

- Yandex Direct API overview: https://yandex.ru/dev/direct/doc/en/concepts/overview
- Yandex Direct OAuth token docs: https://yandex.ru/dev/direct/doc/en/concepts/auth-token
- Yandex OAuth authorization code URL: https://yandex.ru/dev/id/doc/en/codes/code-url
- Search API v2 protobuf contract: https://raw.githubusercontent.com/yandex-cloud/cloudapi/master/yandex/cloud/searchapi/v2/search_service.proto
- Web Search IAM role: https://github.com/yandex-cloud/docs/blob/master/en/_roles/search-api/webSearch/user.md

If Yandex changes the UI or OAuth flow, prefer the official docs above over this local guide.

## 1. Create a Yandex OAuth application

1. Open the Yandex OAuth app console: https://oauth.yandex.ru/client/new
2. Create an application for DirectPilot.
3. Add the required permissions/scopes for Yandex Direct API access.
4. If you will use Metrika endpoints, also prepare Metrika API access separately.
5. Set redirect URI to:
   ```text
   https://oauth.yandex.ru/verification_code
   ```
6. Save the application credentials locally in `.env`:
   ```env
   YANDEX_CLIENT_ID=your_client_id_here
   YANDEX_CLIENT_SECRET=your-client-secret-here
   YANDEX_REDIRECT_URI=https://oauth.yandex.ru/verification_code
   ```

The application may also need Yandex Direct API access enabled/approved for the account. DirectPilot cannot bypass Yandex account/API access restrictions.

## 2. Get a Direct OAuth token

### Option A — manual token flow for local testing

Open this URL after replacing the client id:

```text
https://oauth.yandex.ru/authorize?response_type=token&client_id=YANDEX_CLIENT_ID
```

Authorize the app under the Yandex account that has access to the target Direct account. Copy only the resulting token value into local `.env`:

```env
YANDEX_OAUTH_TOKEN=your-direct-oauth-token-here
```

Do not paste the token into chat, issues, PRs, logs, screenshots, or documentation.

### Option B — authorization-code flow

Use this when you want the standard code exchange flow.

1. Open the authorization URL:
   ```text
   https://oauth.yandex.ru/authorize?response_type=code&client_id=YANDEX_CLIENT_ID&redirect_uri=https%3A%2F%2Foauth.yandex.ru%2Fverification_code
   ```
2. Copy the short-lived authorization code from the Yandex verification page.
3. Exchange the code locally:
   ```bash
   curl -sS -X POST https://oauth.yandex.ru/token \
     -u "$YANDEX_CLIENT_ID:$YANDEX_CLIENT_SECRET" \
     -d grant_type=authorization_code \
     -d code="$YANDEX_AUTH_CODE"
   ```
4. Copy only the returned `access_token` into `.env` as `YANDEX_OAUTH_TOKEN`.

Keep `YANDEX_AUTH_CODE` and the response private. Authorization codes are also secrets.

## 3. Configure Search API v2 (Wordstat + Web Search)

Wordstat and Web Search in DirectPilot use Yandex Search API v2 credentials, not the Direct OAuth token.

Set:

```env
YANDEX_SEARCH_API_KEY=your-search-api-key-here
YANDEX_SEARCH_FOLDER_ID=your-cloud-folder-id-here
```

The key supports both `/wordstat/*` and `/search/web`. The configured folder is an operational prerequisite for `/search/web`; DirectPilot never accepts it from HTTP query/body input. Web Search is read-only but consumes provider quota/billing and requires the IAM role `search-api.webSearch.user` (official role link above). There is no automatic upstream smoke call on startup. Never return or log the API key, folder ID, Authorization header, provider raw XML, or `rawData`.

Leave these blank if you only need Direct/Metrika routes and do not need `/wordstat/*` or `/search/web`.

## 4. Configure Metrika

Metrika endpoints use a separate OAuth token:

```env
YANDEX_METRIKA_OAUTH_TOKEN=your-metrika-oauth-token-here
```

This is not the same value as `YANDEX_OAUTH_TOKEN`. Leave it blank if `/metrika/*` routes are not needed.

## 5. Verify locally through DirectPilot

Start the API:

```bash
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Check health/status without exposing secrets:

```bash
curl -sS http://127.0.0.1:8000/health
curl -sS http://127.0.0.1:8000/integrations/yandex/direct/status
```

Then verify read-only Direct access:

```bash
curl -sS http://127.0.0.1:8000/yandex/campaigns
```

Expected behavior:

- configured credentials return real read-only data with safe/masked status fields;
- missing credentials fail with normalized errors;
- no endpoint should return full tokens, client secrets, auth headers, or raw secret-bearing upstream responses.

## 6. Safety reminders

- Default mode should stay `DIRECTPILOT_MODE=live_readonly`.
- A token alone must not enable writes.
- Real writes require all write gates described in `docs/SAFETY_MODEL.md`.
- `dry_run=true` must never mutate external Yandex state.
- If credentials fail, fix local `.env` or Yandex account access; do not hardcode tokens in source files or tests.
