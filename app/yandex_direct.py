from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings

SANDBOX_BASE_URL = "https://api-sandbox.direct.yandex.com/json/v5"
LIVE_BASE_URL = "https://api.direct.yandex.com/json/v5"


class YandexDirectError(RuntimeError):
    pass


class YandexDirectClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.Client(timeout=20, transport=transport)

    @property
    def base_url(self) -> str:
        if self.settings.directpilot_mode == "sandbox":
            return SANDBOX_BASE_URL
        return LIVE_BASE_URL

    def clients_get(self) -> dict[str, Any]:
        payload = {
            "method": "get",
            "params": {
                "SelectionCriteria": {},
                "FieldNames": ["Login", "ClientId"],
            },
        }
        return self._call("clients", payload)

    def campaigns_get(self) -> dict[str, Any]:
        payload = {
            "method": "get",
            "params": {
                "SelectionCriteria": {},
                "FieldNames": ["Id", "Name", "Status", "State", "Type"],
            },
        }
        return self._call("campaigns", payload)

    def _call(self, service: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.yandex_oauth_token:
            raise YandexDirectError("YANDEX_OAUTH_TOKEN is required for Yandex Direct API calls")

        response = self._client.post(
            f"{self.base_url}/{service}",
            json=payload,
            headers={
                "Authorization": f"Bearer {self.settings.yandex_oauth_token}",
                "Accept-Language": "ru",
            },
        )
        units = response.headers.get("Units")
        body = response.json()
        if "error" in body:
            return {"ok": False, "error": body["error"], "units": units}
        return {"ok": True, "result": body.get("result"), "units": units}
