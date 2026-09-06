from __future__ import annotations

import json

import httpx

from app.modules.integrations.yandex.account_provider import (
    HttpxYandexDirectAccountProvider,
)


def test_clients_get_discovery_uses_exact_fixed_request_without_client_login() -> None:
    captured: list[httpx.Request] = []
    holder_row = {
        "ClientId": 123456789,
        "Login": "synthetic-holder-login",
        "ClientInfo": "Synthetic Holder",
        "Type": "CLIENT",
        "Archived": "NO",
        "CountryId": 225,
        "Currency": "RUB",
        "Grants": [],
        "Representatives": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"result": {"Clients": [holder_row]}})

    provider = HttpxYandexDirectAccountProvider(
        access_token="synthetic-discovery-access-token",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    )

    page = provider.clients_get()

    assert page.client_rows == (holder_row,)
    assert page.limited_by is None
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.direct.yandex.com/json/v5/clients"
    assert request.headers["Authorization"] == "Bearer synthetic-discovery-access-token"
    assert request.headers["Content-Type"] == "application/json; charset=utf-8"
    assert request.headers["Accept-Language"] == "en"
    assert "Client-Login" not in request.headers
    assert json.loads(request.content) == {
        "method": "get",
        "params": {
            "FieldNames": [
                "ClientId",
                "Login",
                "ClientInfo",
                "Type",
                "Archived",
                "CountryId",
                "Currency",
                "Grants",
                "Representatives",
            ]
        },
    }


def test_agencyclients_get_uses_fixed_page_and_never_client_login() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"result": {"Clients": [], "LimitedBy": 10_000}})

    provider = HttpxYandexDirectAccountProvider(
        access_token="synthetic-discovery-access-token",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    )

    page = provider.agencyclients_get(offset=0)

    assert page.client_rows == ()
    assert page.limited_by == 10_000
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.direct.yandex.com/json/v5/agencyclients"
    assert "Client-Login" not in request.headers
    assert json.loads(request.content) == {
        "method": "get",
        "params": {
            "SelectionCriteria": {},
            "FieldNames": [
                "ClientId",
                "Login",
                "ClientInfo",
                "Archived",
                "CountryId",
                "Currency",
                "Grants",
                "Representatives",
                "Type",
            ],
            "Page": {"Limit": 10_000, "Offset": 0},
        },
    }


def test_provider_retries_one_direct_temporary_failure_without_leaking_body() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, json={"error": {"error_code": 52}})
        return httpx.Response(200, json={"result": {"Clients": []}})

    provider = HttpxYandexDirectAccountProvider(
        access_token="synthetic-discovery-access-token",
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )

    assert provider.clients_get().client_rows == ()
    assert attempts == 2
    assert sleeps == [0.1]
