from __future__ import annotations

import inspect

import httpx
import pytest

from app.modules.integrations.yandex.account_provider import (
    DirectAccountDiscoveryProviderFailure,
    HttpxYandexDirectAccountProvider,
)
from app.modules.integrations.yandex.accounts import AccountDiscoveryService


@pytest.mark.parametrize(
    "first_response",
    [
        lambda: httpx.Response(500, content=b"not-a-json-response"),
        lambda: httpx.Response(503, content=b"not-a-json-response"),
        lambda: httpx.Response(200, json={"error": {"error_code": 52}}),
        lambda: httpx.Response(200, json={"error": {"error_code": 506}}),
        lambda: httpx.Response(200, json={"error": {"error_code": 1000}}),
    ],
)
def test_provider_retries_each_allowed_transient_result_once(
    first_response: object,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return first_response()  # type: ignore[operator]
        return httpx.Response(200, json={"result": {"Clients": []}})

    provider = HttpxYandexDirectAccountProvider(
        access_token="synthetic-discovery-access-token",
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )

    assert provider.clients_get().client_rows == ()
    assert attempts == 2
    assert sleeps == [0.1]


def test_provider_retries_transport_failure_once() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("synthetic transport failure")
        return httpx.Response(200, json={"result": {"Clients": []}})

    provider = HttpxYandexDirectAccountProvider(
        access_token="synthetic-discovery-access-token",
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )

    assert provider.clients_get().client_rows == ()
    assert attempts == 2
    assert sleeps == [0.1]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"error": {"error_code": 53}}),
        httpx.Response(200, json={"error": {"error_code": 54}}),
        httpx.Response(206, json={"result": {"Clients": []}}),
        httpx.Response(401, json={"error": {"error_code": 53}}),
    ],
)
def test_provider_never_retries_malformed_auth_permission_or_partial_content(
    response: httpx.Response,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        return response

    provider = HttpxYandexDirectAccountProvider(
        access_token="synthetic-discovery-access-token",
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )

    with pytest.raises(DirectAccountDiscoveryProviderFailure) as raised:
        provider.clients_get()

    assert attempts == 1
    assert sleeps == []
    assert "synthetic-discovery-access-token" not in repr(provider)
    assert "synthetic-discovery-access-token" not in str(raised.value)
    assert "not-json" not in repr(raised.value)


def test_provider_rejects_duplicate_json_keys_without_retry_or_raw_body_output() -> None:
    attempts = 0
    raw_login = "synthetic-raw-provider-login"
    body = (
        '{"result":{"Clients":[]},"result":{"Clients":['
        '{"Login":"' + raw_login + '"}]}}'
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, content=body)

    provider = HttpxYandexDirectAccountProvider(
        access_token="synthetic-discovery-access-token",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    )

    with pytest.raises(DirectAccountDiscoveryProviderFailure) as raised:
        provider.clients_get()

    assert attempts == 1
    assert raised.value.kind == "provider_malformed_response"
    assert raw_login not in str(raised.value)
    assert raw_login not in repr(raised.value)


def test_provider_rejects_explicit_null_limited_by_without_retry() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json={"result": {"Clients": [], "LimitedBy": None}})

    provider = HttpxYandexDirectAccountProvider(
        access_token="synthetic-discovery-access-token",
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )

    with pytest.raises(DirectAccountDiscoveryProviderFailure) as raised:
        provider.agencyclients_get(offset=0)

    assert raised.value.kind == "provider_malformed_response"
    assert attempts == 1
    assert sleeps == []


@pytest.mark.parametrize("non_rfc_constant", ["NaN", "Infinity", "-Infinity"])
def test_provider_rejects_non_rfc_json_constants_without_retry_or_content_leak(
    non_rfc_constant: str,
) -> None:
    attempts = 0
    sleeps: list[float] = []
    raw_marker = "synthetic-non-rfc-provider-login"
    body = (
        '{"result":{"Clients":[{"ClientId":123456789,"Login":"'
        + raw_marker
        + '","ClientInfo":"Synthetic non-RFC account","Type":"CLIENT",'
        '"Archived":"NO","Grants":'
        + non_rfc_constant
        + "}]}}"
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, content=body)

    provider = HttpxYandexDirectAccountProvider(
        access_token="synthetic-discovery-access-token",
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    )

    with pytest.raises(DirectAccountDiscoveryProviderFailure) as raised:
        provider.clients_get()

    assert raised.value.kind == "provider_malformed_response"
    assert attempts == 1
    assert sleeps == []
    assert _failure_is_redacted(failure=raised.value, raw_marker=raw_marker) is True


def _failure_is_redacted(
    *,
    failure: DirectAccountDiscoveryProviderFailure,
    raw_marker: str,
) -> bool:
    return raw_marker not in str(failure) and raw_marker not in repr(failure)


def test_provider_uses_explicit_ten_second_timeout() -> None:
    observed_timeout: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed_timeout.update(request.extensions["timeout"])
        return httpx.Response(200, json={"result": {"Clients": []}})

    provider = HttpxYandexDirectAccountProvider(
        access_token="synthetic-discovery-access-token",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    )

    provider.clients_get()

    assert observed_timeout == {
        "connect": 10.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }


def test_discovery_boundaries_do_not_accept_caller_supplied_login_or_client_login() -> None:
    provider_parameters = inspect.signature(HttpxYandexDirectAccountProvider).parameters
    service_parameters = inspect.signature(AccountDiscoveryService.discover).parameters

    assert {"login", "client_login", "clientlogin"}.isdisjoint(provider_parameters)
    assert {"login", "client_login", "clientlogin"}.isdisjoint(service_parameters)
    assert not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in (*provider_parameters.values(), *service_parameters.values())
    )
