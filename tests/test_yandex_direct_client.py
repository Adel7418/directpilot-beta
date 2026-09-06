import httpx
import pytest

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
    settings = Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token=None)
    client = YandexDirectClient(settings=settings, transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    try:
        client.clients_get()
    except YandexDirectError as exc:
        assert "YANDEX_OAUTH_TOKEN" in str(exc)
    else:
        raise AssertionError("expected missing token error")


def test_explicit_connection_credential_does_not_fall_back_to_settings_token():
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["client_login"] = request.headers.get("Client-Login")
        captured["operator_units"] = request.headers.get("Use-Operator-Units")
        return httpx.Response(200, json={"result": {}})

    settings = Settings(
        _env_file=None,
        directpilot_mode="sandbox",
        yandex_oauth_token="legacy-settings-sentinel",
    )
    client = YandexDirectClient(
        settings=settings,
        access_token="connection-credential-for-test",
        client_login=None,
        use_operator_units=False,
        transport=httpx.MockTransport(handler),
    )

    client.clients_get()

    assert captured["authorization"] == "Bearer connection-credential-for-test"
    assert captured["client_login"] is None
    assert captured["operator_units"] is None


def test_explicit_connection_client_redacts_raw_provider_error_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "error": {
                    "error_code": 58,
                    "error_detail": "synthetic-provider-detail",
                    "error_string": "synthetic-provider-string",
                }
            },
        )

    client = YandexDirectClient(
        settings=Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token=None),
        access_token="connection-credential-for-test",
        transport=httpx.MockTransport(handler),
    )

    result = client.clients_get()

    assert result["error"]["error_code"] == 58
    assert "error_detail" not in result["error"]
    assert "error_string" not in result["error"]


def test_connection_scoped_write_is_blocked_before_lease_verifier_or_transport():
    calls = {"verifier": 0, "transport": 0}

    def verifier() -> None:
        calls["verifier"] += 1

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["transport"] += 1
        return httpx.Response(200, json={"result": {}})

    client = YandexDirectClient(
        settings=Settings(_env_file=None, directpilot_mode="live_write", yandex_oauth_token=None),
        access_token="connection-credential-for-test",
        request_context=verifier,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(YandexDirectError):
        client.campaigns_add([])

    assert calls == {"verifier": 0, "transport": 0}


def test_connection_scoped_client_close_is_idempotent_and_never_uses_legacy_fallback():
    transport_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal transport_calls
        transport_calls += 1
        return httpx.Response(200, json={"result": {}})

    client = YandexDirectClient(
        settings=Settings(
            _env_file=None,
            directpilot_mode="sandbox",
            yandex_oauth_token="legacy-settings-sentinel",
        ),
        access_token="connection-credential-for-test",
        transport=httpx.MockTransport(handler),
    )

    client.close()
    client.close()

    with pytest.raises(YandexDirectError):
        client.clients_get()
    assert transport_calls == 0


def test_connection_scoped_account_balance_is_rejected_before_verifier_or_transport() -> None:
    calls = {"verifier": 0, "transport": 0}

    def verifier() -> None:
        calls["verifier"] += 1

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["transport"] += 1
        raise AssertionError("connection-scoped account balance reached transport")

    client = YandexDirectClient(
        settings=Settings(
            _env_file=None,
            directpilot_mode="sandbox",
            yandex_oauth_token="legacy-settings-sentinel",
        ),
        access_token="connection-credential-for-test",
        request_context=verifier,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(YandexDirectError):
        client.account_balance()
    with pytest.raises(YandexDirectError):
        client.account_balance(login="synthetic-caller-login")
    assert calls == {"verifier": 0, "transport": 0}

    client.close()

    with pytest.raises(YandexDirectError):
        client.account_balance()
    with pytest.raises(YandexDirectError):
        client.account_balance(login="synthetic-caller-login")
    assert calls == {"verifier": 0, "transport": 0}
