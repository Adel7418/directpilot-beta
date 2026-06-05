from __future__ import annotations

from app.config import Settings
from app.yandex_direct import YandexDirectClient, YandexDirectError


def check_yandex_direct(settings: Settings) -> dict:
    if settings.directpilot_mode == "mock":
        return {"ok": True, "mode": "mock", "message": "Yandex Direct API check skipped in mock mode"}
    try:
        return YandexDirectClient(settings).clients_get()
    except YandexDirectError as exc:
        return {"ok": False, "error": {"type": "YandexDirectError", "message": str(exc)}}
    except Exception as exc:
        return {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
