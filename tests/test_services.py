from app.config import Settings
from app.services import check_yandex_direct
import app.services as services


def test_check_yandex_direct_skips_network_in_mock_mode():
    settings = Settings(_env_file=None, directpilot_mode="mock")

    result = check_yandex_direct(settings)

    assert result == {
        "ok": True,
        "mode": "mock",
        "message": "Yandex Direct API check skipped in mock mode",
    }


def test_check_yandex_direct_returns_structured_error_on_unexpected_failure(monkeypatch):
    class BrokenClient:
        def __init__(self, settings):
            pass

        def clients_get(self):
            raise RuntimeError("network exploded")

    monkeypatch.setattr(services, "YandexDirectClient", BrokenClient)
    settings = Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token="token")

    result = check_yandex_direct(settings)

    assert result["ok"] is False
    assert result["error"]["type"] == "RuntimeError"
    assert result["error"]["message"] == "network exploded"
