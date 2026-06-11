"""Tests for read-only Direct balance (Live v4) and Direct campaign finance (v5).

All tests use httpx.MockTransport — no real network calls. The goal of
this module is to lock down:

- account_balance() targets https://api.direct.yandex.ru/live/v4/json/ as
  a single POST with body {token, method=AccountManagement, param[Action=Get,
  SelectionCriteria[Logins, AccountIDS]]} — i.e. the documented Live v4
  AccountManagement envelope, NOT the v5 JSON-RPC envelope.
- The Live v4 response is parsed into a list of YandexAccountBalance dicts
  exposing Amount, AmountAvailableForTransfer, Currency, AccountDayBudget.
- No token or other secret material is ever echoed back to the caller.
- campaigns_get_finance() targets the v5 JSON-RPC campaigns service with
  FieldNames = Id, Name, Status, State, Type, DailyBudget, Funds, Statistics,
  StartDate, EndDate and returns a list of YandexCampaignFinance dicts with
  spend in micro-units surfaced separately from the display amount.
- Missing OAUTH token → YandexDirectError before any network call.
- HTTP error / transport error → YandexDirectError with the HTTP status only,
  body never echoed.
- FastAPI endpoints:
  - GET /yandex/account/balance?login=... → mocked call, source=yandex,
    read_only=true, never includes the token in the body.
  - GET /yandex/account/balance without ?login → falls back to clients.get.
  - GET /yandex/campaigns/finance → mocked call returning FinanceRow list.
  - Missing YANDEX_OAUTH_TOKEN → 503.
  - Upstream error → 502 with redacted message.
  - Mock mode (directpilot_mode=mock) → 409 conflict (live_read endpoints
    require a credentialed mode).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.yandex_direct import YandexDirectClient, YandexDirectError


SECRET_TOKEN = "TEST-OAUTH-TOKEN-1234"


def _settings(mode: str = "live_readonly", **overrides: Any) -> Settings:
    base: dict[str, Any] = {"_env_file": None, "directpilot_mode": mode, "yandex_oauth_token": SECRET_TOKEN}
    base.update(overrides)
    return Settings(**base)


def _client(handler, mode: str = "live_readonly", **overrides: Any) -> YandexDirectClient:
    return YandexDirectClient(
        settings=_settings(mode, **overrides),
        transport=httpx.MockTransport(handler),
    )


# ---------------------------------------------------------------------------
# account_balance — Live v4 AccountManagement
# ---------------------------------------------------------------------------


def _live_v4_handler(status: int = 200, body: Any = None) -> tuple[dict, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode()) if request.content else None
        return httpx.Response(status, json=body if body is not None else {})

    return captured, handler


def test_account_balance_posts_to_live_v4_account_management_envelope():
    captured, handler = _live_v4_handler(
        body={
            "data": [
                {
                    "Amount": 1234.56,
                    "AmountAvailableForTransfer": 1000.0,
                    "Currency": "RUB",
                    "AccountDayBudget": {
                        "Amount": 500.0,
                        "SpendMode": "DEFAULT",
                    },
                    "Login": "agent-agency",
                }
            ]
        }
    )
    client = _client(handler)

    result = client.account_balance(login="agent-agency")

    # Live v4 AccountManagement uses the legacy POST URL (NOT the v5
    # json/v5/{service} URL).
    assert captured["url"] == "https://api.direct.yandex.ru/live/v4/json/"
    assert captured["method"] == "POST"
    # Body must follow the Live v4 envelope: {token, method, param} (NOT the
    # v5 JSON-RPC envelope of {method, params}).
    body = captured["body"]
    assert body["token"] == SECRET_TOKEN
    assert body["method"] == "AccountManagement"
    assert body["param"]["Action"] == "Get"
    assert body["param"]["SelectionCriteria"]["Logins"] == ["agent-agency"]
    assert body["param"]["SelectionCriteria"]["AccountIDS"] == []
    # OAuth bearer must NOT be used here — Live v4 expects `token` in body.
    assert "Bearer" not in captured["headers"].get("authorization", "")
    # The result envelope is the Live v4 one: {ok, data}.
    assert result["ok"] is True
    assert isinstance(result["data"], list)
    assert result["data"][0]["Currency"] == "RUB"
    # Token must never be echoed back to the caller.
    assert SECRET_TOKEN not in str(result)


def test_account_balance_without_login_omits_logins_but_keeps_envelope():
    captured, handler = _live_v4_handler(body={"data": []})
    client = _client(handler)

    result = client.account_balance()

    body = captured["body"]
    assert body["method"] == "AccountManagement"
    assert body["param"]["Action"] == "Get"
    # When the caller does not pass a login, SelectionCriteria.Logins must
    # still be present (Live v4 expects the key) but the list is empty.
    assert body["param"]["SelectionCriteria"]["Logins"] == []
    assert body["param"]["SelectionCriteria"]["AccountIDS"] == []
    assert result["ok"] is True
    assert result["data"] == []


def test_account_balance_extracts_accounts_from_live_v4_data_envelope():
    """Live v4 may return data as {Accounts, ActionsResult}, not a list."""
    captured, handler = _live_v4_handler(
        body={
            "data": {
                "Accounts": [
                    {
                        "Login": "flora-adel963",
                        "Amount": "2781.27",
                        "AmountAvailableForTransfer": "2770.65",
                        "Currency": "RUB",
                        "AccountDayBudget": None,
                    }
                ],
                "ActionsResult": [],
            }
        }
    )
    client = _client(handler)

    result = client.account_balance(login="flora-adel963")

    assert captured["body"]["method"] == "AccountManagement"
    assert result["ok"] is True
    assert result["data"] == [
        {
            "Login": "flora-adel963",
            "Amount": "2781.27",
            "AmountAvailableForTransfer": "2770.65",
            "Currency": "RUB",
            "AccountDayBudget": None,
        }
    ]
    assert SECRET_TOKEN not in str(result)


def test_account_balance_redacts_response_body_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": f"forbidden: token {SECRET_TOKEN} rejected"},
        )

    client = _client(handler)

    with pytest.raises(YandexDirectError) as exc:
        client.account_balance(login="x")

    message = str(exc.value)
    # Secret token is never echoed in the error message.
    assert SECRET_TOKEN not in message
    # The raw response body is never echoed either.
    assert "forbidden" not in message
    # Only the HTTP status is surfaced.
    assert "403" in message


def test_account_balance_raises_before_network_when_token_missing():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        raise AssertionError("network must not be reached without an OAUTH token")

    client = _client(handler, yandex_oauth_token=None)

    with pytest.raises(YandexDirectError) as exc:
        client.account_balance()

    assert "YANDEX_OAUTH_TOKEN" in str(exc.value)
    assert SECRET_TOKEN not in str(exc.value)


# ---------------------------------------------------------------------------
# campaigns_get_finance — Direct API v5 with Funds / Statistics
# ---------------------------------------------------------------------------


def test_campaigns_get_finance_requests_full_finance_field_set():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "result": {
                    "Campaigns": [
                        {
                            "Id": 12345,
                            "Name": "Brand Search",
                            "Status": "ACCEPTED",
                            "State": "ON",
                            "Type": "TEXT_CAMPAIGN",
                            "DailyBudget": {"Amount": 5_000_000, "SpendMode": "STANDARD"},
                            "Funds": {
                                "Mode": "SHARED_ACCOUNT_FUNDS",
                                "CampaignFunds": {
                                    "Balance": 12_345_678,
                                    "DailyBudget": {"Amount": 1_000_000},
                                },
                            },
                            "Statistics": {
                                "Shows": 1_000,
                                "Clicks": 50,
                                "Cost": 12_345_678,
                            },
                            "StartDate": "2026-01-15",
                            "EndDate": "2026-12-31",
                        }
                    ]
                }
            },
        )

    client = _client(handler)
    result = client.campaigns_get_finance()

    assert captured["url"] == "https://api.direct.yandex.com/json/v5/campaigns"
    body = captured["body"]
    assert body["method"] == "get"
    assert body["params"]["SelectionCriteria"] == {}
    assert body["params"]["FieldNames"] == [
        "Id",
        "Name",
        "Status",
        "State",
        "Type",
        "DailyBudget",
        "Funds",
        "Statistics",
        "StartDate",
        "EndDate",
    ]
    assert result["ok"] is True
    finance = result["data"]
    assert len(finance) == 1
    row = finance[0]
    assert row["id"] == "12345"
    assert row["name"] == "Brand Search"
    assert row["status"] == "ACCEPTED"
    assert row["state"] == "ON"
    assert row["type"] == "TEXT_CAMPAIGN"
    # DailyBudget is in micro-units (1/1_000_000 of currency). We surface
    # both the raw micro value and the display value so callers can pick.
    assert row["daily_budget_micros"] == 5_000_000
    assert row["daily_budget"] == 5.0
    # Funds.Balance micro-units.
    assert row["funds_balance_micros"] == 12_345_678
    assert row["funds_balance"] == 12.345678
    # Statistics.Cost micro-units.
    assert row["spend_micros"] == 12_345_678
    assert row["spend"] == 12.345678
    assert row["statistics_shows"] == 1_000
    assert row["statistics_clicks"] == 50
    assert row["start_date"] == "2026-01-15"
    assert row["end_date"] == "2026-12-31"
    # No secret material in the result.
    assert SECRET_TOKEN not in str(result)


def test_campaigns_get_finance_handles_missing_blocks_gracefully():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "result": {
                    "Campaigns": [
                        {
                            "Id": 99,
                            "Name": "Empty",
                            "Status": "DRAFT",
                        }
                    ]
                }
            },
        )

    client = _client(handler)
    result = client.campaigns_get_finance()

    assert result["ok"] is True
    row = result["data"][0]
    assert row["id"] == "99"
    # Missing blocks must NOT crash the parser — all money values default to 0.
    assert row["daily_budget_micros"] == 0
    assert row["funds_balance_micros"] == 0
    assert row["spend_micros"] == 0
    assert row["statistics_shows"] == 0
    assert row["statistics_clicks"] == 0
    assert row["start_date"] is None
    assert row["end_date"] is None


def test_campaigns_get_finance_raises_before_network_when_token_missing():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        raise AssertionError("network must not be reached without an OAUTH token")

    client = _client(handler, yandex_oauth_token=None)

    with pytest.raises(YandexDirectError) as exc:
        client.campaigns_get_finance()

    assert "YANDEX_OAUTH_TOKEN" in str(exc.value)


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------


def _override_settings(mode: str = "live_readonly") -> None:
    from app import main as main_mod

    app.dependency_overrides[main_mod.get_settings] = lambda: _settings(mode)


def _override_client(client_obj) -> None:
    from app import main as main_mod

    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj


def _clear_overrides() -> None:
    from app import main as main_mod

    app.dependency_overrides.pop(main_mod.get_settings, None)
    app.dependency_overrides.pop(main_mod.get_yandex_client, None)


def test_balance_endpoint_returns_envelope_without_login_uses_clients_get():
    captured_clients: dict[str, Any] = {}

    def clients_handler(request: httpx.Request) -> httpx.Response:
        captured_clients["url"] = str(request.url)
        captured_clients["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"result": {"Clients": [{"Login": "agency-login", "ClientId": "1"}]}},
        )

    def balance_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "Amount": 9_999.99,
                        "AmountAvailableForTransfer": 1_000.0,
                        "Currency": "RUB",
                        "AccountDayBudget": {"Amount": 500.0, "SpendMode": "DEFAULT"},
                    }
                ]
            },
        )

    # We can't mock two endpoints with one handler, so use a list of handlers.
    handler_calls: list[httpx.Request] = []

    def dispatch(request: httpx.Request) -> httpx.Response:
        handler_calls.append(request)
        # Route based on URL: clients.get vs live v4 balance.
        if "live/v4" in str(request.url):
            return balance_handler(request)
        return clients_handler(request)

    client_obj = YandexDirectClient(
        settings=_settings(),
        transport=httpx.MockTransport(dispatch),
    )
    _override_settings("live_readonly")
    _override_client(client_obj)
    try:
        response = TestClient(app).get("/yandex/account/balance")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    # The first call (clients.get) provided the login used to scope balance.
    assert captured_clients["url"] == "https://api.direct.yandex.com/json/v5/clients"
    assert body["accounts"][0]["currency"] == "RUB"
    assert body["accounts"][0]["amount"] == 9_999.99
    # Token is never echoed back.
    assert SECRET_TOKEN not in response.text


def test_balance_endpoint_with_login_calls_live_v4_directly():
    handler_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        handler_calls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"Amount": 1.0, "Currency": "RUB"}]})

    client_obj = YandexDirectClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    _override_settings("live_readonly")
    _override_client(client_obj)
    try:
        response = TestClient(app).get("/yandex/account/balance?login=my-login")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    # Only one request — the Live v4 balance call. No clients.get.
    assert any("live/v4" in u for u in handler_calls)
    assert not any(u.endswith("/clients") for u in handler_calls)
    body = response.json()
    # The endpoint exposes snake_case fields (Pydantic-friendly). The
    # `Amount` field from Live v4 surfaces as `amount` here.
    assert body["accounts"][0]["amount"] == 1.0


def test_balance_endpoint_returns_503_when_token_missing():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        raise AssertionError("network must not be reached without token")

    client_obj = YandexDirectClient(
        settings=_settings(yandex_oauth_token=None),
        transport=httpx.MockTransport(handler),
    )
    _override_settings("live_readonly")
    _override_client(client_obj)
    try:
        response = TestClient(app).get("/yandex/account/balance?login=x")
    finally:
        _clear_overrides()

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["error_type"] == "YandexDirectError"
    assert "YANDEX_OAUTH_TOKEN" in detail["message"]
    assert SECRET_TOKEN not in response.text


def test_balance_endpoint_returns_502_on_upstream_error_without_token_echo():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": f"forbidden: token {SECRET_TOKEN}"},
        )

    client_obj = YandexDirectClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    _override_settings("live_readonly")
    _override_client(client_obj)
    try:
        response = TestClient(app).get("/yandex/account/balance?login=x")
    finally:
        _clear_overrides()

    assert response.status_code == 502, response.text
    assert SECRET_TOKEN not in response.text


def test_finance_endpoint_returns_finance_rows_with_micro_units():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "Campaigns": [
                        {
                            "Id": 1,
                            "Name": "C1",
                            "Status": "ACCEPTED",
                            "State": "ON",
                            "Type": "TEXT_CAMPAIGN",
                            "DailyBudget": {"Amount": 1_000_000},
                            "Funds": {"CampaignFunds": {"Balance": 2_000_000}},
                            "Statistics": {"Shows": 100, "Clicks": 5, "Cost": 250_000},
                            "StartDate": "2026-01-01",
                            "EndDate": "2026-12-31",
                        }
                    ]
                }
            },
        )

    client_obj = YandexDirectClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    _override_settings("live_readonly")
    _override_client(client_obj)
    try:
        response = TestClient(app).get("/yandex/campaigns/finance")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["id"] == "1"
    assert row["daily_budget_micros"] == 1_000_000
    assert row["funds_balance_micros"] == 2_000_000
    assert row["spend_micros"] == 250_000
    assert SECRET_TOKEN not in response.text


def test_finance_endpoint_returns_503_when_token_missing():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        raise AssertionError("network must not be reached without token")

    client_obj = YandexDirectClient(
        settings=_settings(yandex_oauth_token=None),
        transport=httpx.MockTransport(handler),
    )
    _override_settings("live_readonly")
    _override_client(client_obj)
    try:
        response = TestClient(app).get("/yandex/campaigns/finance")
    finally:
        _clear_overrides()

    assert response.status_code == 503, response.text
    assert SECRET_TOKEN not in response.text


def test_finance_endpoint_returns_502_on_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": f"internal: token {SECRET_TOKEN}"})

    client_obj = YandexDirectClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    _override_settings("live_readonly")
    _override_client(client_obj)
    try:
        response = TestClient(app).get("/yandex/campaigns/finance")
    finally:
        _clear_overrides()

    assert response.status_code == 502, response.text
    assert SECRET_TOKEN not in response.text


def test_balance_endpoint_returns_409_when_client_unavailable():
    """When `get_yandex_client` returns None (mock mode OR no settings),
    the endpoint must short-circuit with 409 before any network call."""
    _clear_overrides()
    from app import main as main_mod
    from app.config import get_settings

    get_settings.cache_clear()
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: None
    try:
        response = TestClient(app).get("/yandex/account/balance?login=x")
    finally:
        _clear_overrides()
        get_settings.cache_clear()

    assert response.status_code == 409, response.text
    assert "requires" in response.text or "mode" in response.text


def test_finance_endpoint_returns_409_when_client_unavailable():
    _clear_overrides()
    from app import main as main_mod
    from app.config import get_settings

    get_settings.cache_clear()
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: None
    try:
        response = TestClient(app).get("/yandex/campaigns/finance")
    finally:
        _clear_overrides()
        get_settings.cache_clear()

    assert response.status_code == 409, response.text
