import httpx

from app.config import Settings
from app.yandex_direct import YandexDirectClient, YandexDirectError


def test_sandbox_base_url_is_used_in_sandbox_mode():
    settings = Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token="token")
    client = YandexDirectClient(settings=settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"result": {}})))

    assert client.base_url == "https://api-sandbox.direct.yandex.com/json/v5"


def test_clients_get_sends_bearer_token_and_redacts_response_errors():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"error": {"error_code": 58, "error_detail": "Незавершенная регистрация"}})

    settings = Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token="secret-token")
    client = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))

    result = client.clients_get()

    assert captured["url"] == "https://api-sandbox.direct.yandex.com/json/v5/clients"
    assert captured["authorization"] == "Bearer secret-token"
    assert '"method":"get"' in captured["body"]
    assert "SelectionCriteria" not in captured["body"]
    assert result["ok"] is False
    assert result["error"]["error_code"] == 58
    assert "secret-token" not in str(result)


def test_missing_token_raises_before_network_call():
    settings = Settings(_env_file=None, directpilot_mode="sandbox")
    client = YandexDirectClient(settings=settings, transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    try:
        client.clients_get()
    except YandexDirectError as exc:
        assert "YANDEX_OAUTH_TOKEN" in str(exc)
    else:
        raise AssertionError("expected missing token error")
