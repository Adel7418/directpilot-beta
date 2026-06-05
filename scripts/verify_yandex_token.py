from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


def mask(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}(len={len(value)})"


def request_json(url: str, token: str, payload: dict | None = None) -> tuple[int, dict | str]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Accept-Language"] = "ru"
    req = urllib.request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body[:500]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body[:500]


def sanitize(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key.lower() in {"access_token", "refresh_token", "client_secret", "token", "client_id", "psuid", "id"}:
                redacted[key] = mask(str(item))
            elif key.lower() in {"login", "default_email", "display_name", "real_name"}:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = sanitize(item)
        return redacted
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def main() -> int:
    load_dotenv()
    token = os.environ.get("YANDEX_OAUTH_TOKEN")
    client_id = os.environ.get("YANDEX_CLIENT_ID")
    print("configured", {"client_id": mask(client_id), "token": mask(token)})
    if not token:
        print("ERROR: YANDEX_OAUTH_TOKEN is missing")
        return 2

    login_status, login_body = request_json("https://login.yandex.ru/info?format=json", token)
    print("login_info_status", login_status)
    print("login_info_body", json.dumps(sanitize(login_body), ensure_ascii=False)[:1000])

    direct_payload = {"method": "get", "params": {"SelectionCriteria": {}, "FieldNames": ["Login", "ClientId"]}}
    direct_status, direct_body = request_json("https://api.direct.yandex.com/json/v5/clients", token, direct_payload)
    print("direct_clients_status", direct_status)
    print("direct_clients_body", json.dumps(sanitize(direct_body), ensure_ascii=False)[:1200])
    return 0 if login_status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
