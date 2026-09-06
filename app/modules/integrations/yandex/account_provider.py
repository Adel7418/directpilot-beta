from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

_DIRECT_API_BASE_URL = "https://api.direct.yandex.com/json/v5"
_DISCOVERY_TIMEOUT_SECONDS = 10.0
_RETRY_DELAY_SECONDS = 0.1
_RETRYABLE_DIRECT_CODES = frozenset({52, 506, 1000})
_CLIENTS_FIELD_NAMES = (
    "ClientId",
    "Login",
    "ClientInfo",
    "Type",
    "Archived",
    "CountryId",
    "Currency",
    "Grants",
    "Representatives",
)
_AGENCY_CLIENTS_FIELD_NAMES = (
    "ClientId",
    "Login",
    "ClientInfo",
    "Archived",
    "CountryId",
    "Currency",
    "Grants",
    "Representatives",
    "Type",
)


class DirectAccountDiscoveryProviderFailure(RuntimeError):
    """Safe failure from the Direct account-discovery HTTP boundary."""

    def __init__(self, kind: str, *, direct_code: int | None = None) -> None:
        self.kind = kind
        self.direct_code = direct_code
        super().__init__("Direct account discovery provider request failed")


@dataclass(frozen=True, slots=True)
class DirectAccountDiscoveryPage:
    """Internal provider page; provider data stays out of repr output."""

    client_rows: tuple[Mapping[str, Any], ...] = field(repr=False)
    limited_by: int | None = None


class HttpxYandexDirectAccountProvider:
    """Fixed, read-only Direct v5 account-discovery boundary."""

    def __init__(
        self,
        *,
        access_token: str,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Direct discovery access token is invalid")
        self._access_token = access_token
        self._transport = transport
        self._sleeper = time.sleep if sleeper is None else sleeper

    def __repr__(self) -> str:
        return "HttpxYandexDirectAccountProvider()"

    def clients_get(self) -> DirectAccountDiscoveryPage:
        return self._post(
            path="/clients",
            payload={
                "method": "get",
                "params": {"FieldNames": list(_CLIENTS_FIELD_NAMES)},
            },
        )

    def agencyclients_get(self, *, offset: int) -> DirectAccountDiscoveryPage:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("Direct agency discovery offset is invalid")
        return self._post(
            path="/agencyclients",
            payload={
                "method": "get",
                "params": {
                    "SelectionCriteria": {},
                    "FieldNames": list(_AGENCY_CLIENTS_FIELD_NAMES),
                    "Page": {"Limit": 10_000, "Offset": offset},
                },
            },
        )

    def _post(self, *, path: str, payload: Mapping[str, object]) -> DirectAccountDiscoveryPage:
        for attempt in range(2):
            try:
                response = self._send(path=path, payload=payload)
            except httpx.HTTPError:
                if attempt == 0:
                    self._sleeper(_RETRY_DELAY_SECONDS)
                    continue
                raise DirectAccountDiscoveryProviderFailure("provider_unavailable") from None

            if 500 <= response.status_code <= 599:
                if attempt == 0:
                    self._sleeper(_RETRY_DELAY_SECONDS)
                    continue
                raise DirectAccountDiscoveryProviderFailure("provider_unavailable")

            document = _strict_json_mapping(response)
            error_code = _direct_error_code(document)
            if error_code is not None:
                if error_code in _RETRYABLE_DIRECT_CODES and attempt == 0:
                    self._sleeper(_RETRY_DELAY_SECONDS)
                    continue
                failure_kind = (
                    "provider_temporary"
                    if error_code in _RETRYABLE_DIRECT_CODES
                    else "provider_error"
                )
                raise DirectAccountDiscoveryProviderFailure(
                    failure_kind,
                    direct_code=error_code,
                )
            if response.status_code != 200:
                raise DirectAccountDiscoveryProviderFailure("provider_http_error")
            return _success_page(document)
        raise AssertionError("unreachable retry state")

    def _send(self, *, path: str, payload: Mapping[str, object]) -> httpx.Response:
        with httpx.Client(
            timeout=httpx.Timeout(_DISCOVERY_TIMEOUT_SECONDS),
            transport=self._transport,
        ) as client:
            return client.post(
                f"{_DIRECT_API_BASE_URL}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept-Language": "en",
                },
            )


class _DuplicateJsonKey(ValueError):
    pass


class _NonRfcJsonConstant(ValueError):
    pass


def _reject_non_rfc_json_constant(value: str) -> object:
    del value
    raise _NonRfcJsonConstant


def _strict_json_mapping(response: httpx.Response) -> Mapping[str, Any]:
    try:
        document = json.loads(
            response.content.decode("utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_non_rfc_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        _NonRfcJsonConstant,
    ):
        raise DirectAccountDiscoveryProviderFailure("provider_malformed_response") from None
    if not isinstance(document, Mapping):
        raise DirectAccountDiscoveryProviderFailure("provider_malformed_response")
    return document


def _json_object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise _DuplicateJsonKey
        document[key] = value
    return document


def _direct_error_code(document: Mapping[str, Any]) -> int | None:
    if "error" not in document:
        return None
    if "result" in document:
        raise DirectAccountDiscoveryProviderFailure("provider_malformed_response")
    error = document["error"]
    if not isinstance(error, Mapping):
        raise DirectAccountDiscoveryProviderFailure("provider_malformed_response")
    error_code = error.get("error_code")
    if not isinstance(error_code, int) or isinstance(error_code, bool):
        raise DirectAccountDiscoveryProviderFailure("provider_malformed_response")
    return error_code


def _success_page(document: Mapping[str, Any]) -> DirectAccountDiscoveryPage:
    result = document.get("result")
    if not isinstance(result, Mapping):
        raise DirectAccountDiscoveryProviderFailure("provider_malformed_response")
    clients = result.get("Clients")
    if not isinstance(clients, list) or not all(isinstance(row, Mapping) for row in clients):
        raise DirectAccountDiscoveryProviderFailure("provider_malformed_response")
    limited_by: int | None = None
    if "LimitedBy" in result:
        limited_by = result["LimitedBy"]
        if not isinstance(limited_by, int) or isinstance(limited_by, bool):
            raise DirectAccountDiscoveryProviderFailure("provider_malformed_response")
    return DirectAccountDiscoveryPage(client_rows=tuple(clients), limited_by=limited_by)
