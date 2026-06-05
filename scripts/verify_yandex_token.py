from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, mask_secret
from app.yandex_direct import YandexDirectClient, YandexDirectError


def sanitize(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key.lower() in {"access_token", "refresh_token", "client_secret", "token", "client_id", "psuid", "id"}:
                redacted[key] = mask_secret(str(item))
            elif key.lower() in {"login", "default_email", "display_name", "real_name"}:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = sanitize(item)
        return redacted
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def main() -> int:
    settings = Settings()
    print(
        "configured",
        {
            "mode": settings.directpilot_mode,
            "client_id": mask_secret(settings.yandex_client_id),
            "token": mask_secret(settings.yandex_oauth_token),
        },
    )
    if not settings.yandex_oauth_token:
        print("ERROR: YANDEX_OAUTH_TOKEN is missing")
        return 2

    with httpx.Client(timeout=20) as http:
        login_resp = http.get(
            "https://login.yandex.ru/info?format=json",
            headers={"Authorization": f"Bearer {settings.yandex_oauth_token}"},
        )
    print("login_info_status", login_resp.status_code)
    print("login_info_body", json.dumps(sanitize(login_resp.json()), ensure_ascii=False)[:1000])

    try:
        direct_client = YandexDirectClient(settings)
        direct_body = direct_client.clients_get()
    except YandexDirectError as exc:
        direct_body = {"ok": False, "error": {"message": str(exc)}}
    print("direct_base_url", direct_client.base_url if 'direct_client' in locals() else None)
    print("direct_clients_body", json.dumps(sanitize(direct_body), ensure_ascii=False)[:1200])
    return 0 if login_resp.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
