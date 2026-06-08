"""Tests for the modern Yandex AI Studio / Search API v2 Wordstat client.

All tests use httpx.MockTransport — no real network calls. The goal of
this module is to lock down:

- HTTP method, URL, headers and JSON body shape for the four v2 wordstat
  endpoints (topRequests, dynamics, regions, getRegionsTree)
- Auth header is `Authorization: Api-Key <key>` when only the API key is
  set (the documented modern v2 path)
- A missing API key raises YandexSearchWordstatError *before* any network
  call and never echoes the key
- An HTTP error response is surfaced as YandexSearchWordstatError and the
  raw body (which can contain key echoes or sensitive query data) is
  *never* included in the exception message
- Optional folderId, regions, devices, numPhrases are forwarded as-is
  when present and omitted from the body when not
- The FastAPI /wordstat/* endpoints:
  - return 503 / 502 with a redacted envelope when the key is missing
  - return source="yandex_search_api" / read_only=true + raw data when
    the client succeeds
  - never include the API key in any response body

This is a strict TDD spec: the tests here are written first and the
production code in app/yandex_search_wordstat.py + app/main.py is
written to make them pass.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.yandex_search_wordstat import (
    YandexSearchWordstatClient,
    YandexSearchWordstatError,
    YandexSearchWordstatMissingKeyError,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


SECRET_KEY = "AQVN-SECRET-WORDSTAT-KEY-1234567890abcdef"
SECRET_FOLDER = "b1g1234567890abcdef"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "yandex_search_api_key": SECRET_KEY,
        "yandex_search_folder_id": SECRET_FOLDER,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _client(handler, **overrides: Any) -> YandexSearchWordstatClient:
    return YandexSearchWordstatClient(
        settings=_settings(**overrides),
        transport=httpx.MockTransport(handler),
    )


def _capture(status: int = 200, body: Any = None) -> tuple[dict, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode()) if request.content else None
        return httpx.Response(status, json=body if body is not None else {})

    return captured, handler


# ---------------------------------------------------------------------------
# construction / auth
# ---------------------------------------------------------------------------


def test_client_uses_configured_base_url():
    assert (
        YandexSearchWordstatClient.BASE_URL
        == "https://searchapi.api.cloud.yandex.net/v2/wordstat"
    )


def test_missing_api_key_raises_before_network():
    """No key configured -> YandexSearchWordstatError, transport never touched."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        raise AssertionError("network must not be reached without an API key")

    client = _client(handler, yandex_search_api_key=None)
    with pytest.raises(YandexSearchWordstatMissingKeyError) as exc:
        client.wordstat_top_requests("ремонт")

    # The error must name the missing setting and never echo a key (there is none).
    assert "YANDEX_SEARCH_API_KEY" in str(exc.value)
    assert SECRET_KEY not in str(exc.value)


# ---------------------------------------------------------------------------
# topRequests
# ---------------------------------------------------------------------------


def test_top_requests_posts_to_top_requests_with_default_limit_and_regions():
    captured, handler = _capture(body={"topRequests": [{"phrase": "ремонт", "count": 1000}]})
    client = _client(handler)

    result = client.wordstat_top_requests("ремонт")

    assert captured["method"] == "POST"
    assert (
        captured["url"]
        == "https://searchapi.api.cloud.yandex.net/v2/wordstat/topRequests"
    )
    # API-key auth, not OAuth.
    assert captured["headers"]["authorization"] == f"Api-Key {SECRET_KEY}"
    # folderId is forwarded when configured on the client.
    assert captured["body"]["folderId"] == SECRET_FOLDER
    assert captured["body"]["phrase"] == "ремонт"
    # 43 is the conventional default for the Moscow region; Search API v2
    # expects region IDs as strings.
    assert captured["body"]["regions"] == ["43"]
    # numPhrases is an int64 string in the Search API v2 schema.
    assert captured["body"]["numPhrases"] == "50"
    # No devices filter when not provided.
    assert "devices" not in captured["body"]
    assert result["ok"] is True
    assert result["data"]["topRequests"][0]["phrase"] == "ремонт"
    assert SECRET_KEY not in str(result)


def test_top_requests_forwards_custom_regions_limit_and_devices():
    captured, handler = _capture(body={"topRequests": []})
    client = _client(handler)

    client.wordstat_top_requests(
        "сантехник",
        region_ids=[213, 1],
        limit=10,
        devices=["desktop", "mobile"],
    )

    assert captured["body"]["regions"] == ["213", "1"]
    assert captured["body"]["numPhrases"] == "10"
    assert captured["body"]["devices"] == ["desktop", "mobile"]


def test_top_requests_uses_per_call_folder_id_override():
    captured, handler = _capture(body={"topRequests": []})
    client = _client(handler, yandex_search_folder_id=None)

    client.wordstat_top_requests("ремонт", folder_id="custom-folder")

    assert captured["body"]["folderId"] == "custom-folder"


# ---------------------------------------------------------------------------
# dynamics
# ---------------------------------------------------------------------------


def test_dynamics_posts_to_dynamics_without_numPhrases():
    captured, handler = _capture(body={"dynamics": []})
    client = _client(handler)

    result = client.wordstat_dynamics(
        "ремонт",
        period="PERIOD_MONTHLY",
        date_from="2026-01-01T00:00:00Z",
        date_to="2026-02-01T00:00:00Z",
        region_ids=[43],
    )

    assert (
        captured["url"]
        == "https://searchapi.api.cloud.yandex.net/v2/wordstat/dynamics"
    )
    # The dynamics endpoint does not accept numPhrases; the client must
    # never include it even if a caller-side default leaks in.
    assert "numPhrases" not in captured["body"]
    assert captured["body"]["phrase"] == "ремонт"
    assert captured["body"]["period"] == "PERIOD_MONTHLY"
    assert captured["body"]["fromDate"] == "2026-01-01T00:00:00Z"
    assert captured["body"]["toDate"] == "2026-02-01T00:00:00Z"
    assert captured["body"]["regions"] == ["43"]
    assert captured["body"]["folderId"] == SECRET_FOLDER
    assert result["ok"] is True


def test_dynamics_forwards_devices_when_provided():
    captured, handler = _capture(body={"dynamics": []})
    client = _client(handler)

    client.wordstat_dynamics(
        "ремонт",
        period="PERIOD_WEEKLY",
        date_from="2026-01-01T00:00:00Z",
        devices=["DEVICE_PHONE"],
    )

    assert captured["body"]["devices"] == ["DEVICE_PHONE"]


# ---------------------------------------------------------------------------
# regions
# ---------------------------------------------------------------------------


def test_regions_distribution_posts_to_regions():
    captured, handler = _capture(body={"regions": []})
    client = _client(handler)

    result = client.wordstat_regions_distribution("ремонт")

    assert (
        captured["url"]
        == "https://searchapi.api.cloud.yandex.net/v2/wordstat/regions"
    )
    assert captured["body"]["phrase"] == "ремонт"
    assert captured["body"]["region"] == "REGION_ALL"
    assert "devices" not in captured["body"]
    assert captured["body"]["folderId"] == SECRET_FOLDER
    assert result["ok"] is True


def test_regions_distribution_forwards_region_enum_and_devices():
    captured, handler = _capture(body={"regions": []})
    client = _client(handler)

    client.wordstat_regions_distribution(
        "ремонт",
        region="REGION_CITIES",
        devices=["DEVICE_ALL"],
    )

    assert captured["body"]["region"] == "REGION_CITIES"
    assert captured["body"]["devices"] == ["DEVICE_ALL"]


# ---------------------------------------------------------------------------
# getRegionsTree
# ---------------------------------------------------------------------------


def test_regions_tree_posts_to_get_regions_tree_without_phrase():
    captured, handler = _capture(body={"regions": []})
    client = _client(handler)

    result = client.wordstat_regions_tree()

    assert (
        captured["url"]
        == "https://searchapi.api.cloud.yandex.net/v2/wordstat/getRegionsTree"
    )
    # No phrase field on this endpoint.
    assert "phrase" not in captured["body"]
    assert captured["body"]["folderId"] == SECRET_FOLDER
    assert result["ok"] is True


def test_regions_tree_honours_per_call_folder_id_override():
    captured, handler = _capture(body={"regions": []})
    client = _client(handler, yandex_search_folder_id=None)

    client.wordstat_regions_tree(folder_id="alt-folder")

    assert captured["body"]["folderId"] == "alt-folder"


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def test_http_error_redacts_key_and_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": f"unauthorized: Api-Key {SECRET_KEY} is invalid"},
        )

    client = _client(handler)

    with pytest.raises(YandexSearchWordstatError) as exc:
        client.wordstat_top_requests("ремонт")

    message = str(exc.value)
    # No echo of the secret key.
    assert SECRET_KEY not in message
    # The body itself is never included in the error message.
    assert "unauthorized" not in message
    # We only surface the HTTP status.
    assert "401" in message


def test_transport_error_redacts_key():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client(handler)

    with pytest.raises(YandexSearchWordstatError) as exc:
        client.wordstat_top_requests("ремонт")

    message = str(exc.value)
    assert SECRET_KEY not in message
    assert "ConnectError" in message or "transport" in message.lower()


# ---------------------------------------------------------------------------
# FastAPI /wordstat/* endpoints
# ---------------------------------------------------------------------------


def _override_with_client(client_obj):
    from app import main as main_mod

    app.dependency_overrides[main_mod.get_settings] = lambda: _settings()
    app.dependency_overrides[main_mod.get_yandex_search_wordstat_client] = (
        lambda: client_obj
    )


def _clear_overrides():
    from app import main as main_mod

    app.dependency_overrides.pop(main_mod.get_settings, None)
    app.dependency_overrides.pop(main_mod.get_yandex_search_wordstat_client, None)


def test_top_endpoint_returns_source_yandex_search_api_and_read_only():
    captured, handler = _capture(body={"topRequests": [{"phrase": "ремонт", "count": 42}]})
    client_obj = YandexSearchWordstatClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get("/wordstat/top?phrase=%D1%80%D0%B5%D0%BC%D0%BE%D0%BD%D1%82")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex_search_api"
    assert body["read_only"] is True
    assert body["service"] == "wordstat"
    assert body["method"] == "topRequests"
    assert body["data"]["topRequests"][0]["count"] == 42
    assert captured["url"].endswith("/v2/wordstat/topRequests")
    # Secret key is never echoed back to the user.
    assert SECRET_KEY not in response.text


def test_top_endpoint_forwards_regions_and_limit():
    captured, handler = _capture(body={"topRequests": []})
    client_obj = YandexSearchWordstatClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get(
            "/wordstat/top"
            "?phrase=%D1%80%D0%B5%D0%BC%D0%BE%D0%BD%D1%82"
            "&regions=43&regions=213&limit=10"
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    assert captured["body"]["regions"] == ["43", "213"]
    assert captured["body"]["numPhrases"] == "10"


def test_top_endpoint_rejects_non_integer_regions_with_422_before_network():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        calls.append(request)
        return httpx.Response(200, json={})

    client_obj = YandexSearchWordstatClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get("/wordstat/top?phrase=foo&regions=abc")
    finally:
        _clear_overrides()

    assert response.status_code == 422, response.text
    assert "regions must contain integer ids" in response.text
    assert calls == []


def test_dynamics_endpoint_forwards_phrase_and_regions():
    captured, handler = _capture(body={"dynamics": []})
    client_obj = YandexSearchWordstatClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get(
            "/wordstat/dynamics"
            "?phrase=%D1%80%D0%B5%D0%BC%D0%BE%D0%BD%D1%82"
            "&date_from=2026-01-01T00:00:00Z"
            "&date_to=2026-02-01T00:00:00Z"
            "&period=PERIOD_MONTHLY"
            "&regions=43"
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    assert captured["url"].endswith("/v2/wordstat/dynamics")
    assert captured["body"]["phrase"] == "ремонт"
    assert captured["body"]["period"] == "PERIOD_MONTHLY"
    assert captured["body"]["fromDate"] == "2026-01-01T00:00:00Z"
    assert captured["body"]["toDate"] == "2026-02-01T00:00:00Z"
    assert captured["body"]["regions"] == ["43"]


def test_regions_endpoint_returns_safe_envelope_without_key():
    captured, handler = _capture(body={"regions": []})
    client_obj = YandexSearchWordstatClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get(
            "/wordstat/regions?phrase=%D1%80%D0%B5%D0%BC%D0%BE%D0%BD%D1%82"
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    assert captured["url"].endswith("/v2/wordstat/regions")
    body = response.json()
    assert body["source"] == "yandex_search_api"
    assert body["read_only"] is True


def test_regions_tree_endpoint_uses_per_endpoint_url():
    captured, handler = _capture(body={"regions": []})
    client_obj = YandexSearchWordstatClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get("/wordstat/regions-tree")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    assert captured["url"].endswith("/v2/wordstat/getRegionsTree")
    assert "phrase" not in captured["body"]


def test_wordstat_endpoints_return_503_when_key_missing():
    """Without YANDEX_SEARCH_API_KEY, every /wordstat/* endpoint must
    short-circuit with a 503 (service not configured) — never a 500
    crash and never an actual HTTP call."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        calls.append(request)
        return httpx.Response(200, json={})

    client_obj = YandexSearchWordstatClient(
        settings=Settings(_env_file=None, yandex_search_api_key=None),
        transport=httpx.MockTransport(handler),
    )
    _override_with_client(client_obj)
    try:
        api = TestClient(app)
        top = api.get("/wordstat/top?phrase=foo")
        dyn = api.get("/wordstat/dynamics?phrase=foo&date_from=2026-01-01T00:00:00Z")
        regions = api.get("/wordstat/regions?phrase=foo")
        tree = api.get("/wordstat/regions-tree")
    finally:
        _clear_overrides()

    assert calls == []
    for resp in (top, dyn, regions, tree):
        assert resp.status_code == 503, resp.text
        detail = resp.json()["detail"]
        assert detail["error_type"] == "YandexSearchWordstatError"
        assert "YANDEX_SEARCH_API_KEY" in detail["message"]
        assert SECRET_KEY not in resp.text


def test_wordstat_endpoints_return_502_on_api_error_with_redaction():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"message": f"server failure: Api-Key {SECRET_KEY}"},
        )

    client_obj = YandexSearchWordstatClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get("/wordstat/top?phrase=foo")
    finally:
        _clear_overrides()

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error_type"] == "YandexSearchWordstatError"
    assert SECRET_KEY not in response.text
    assert "server failure" not in response.text


def test_settings_expose_search_api_key_and_folder_id():
    settings = _settings()
    assert settings.yandex_search_api_key == SECRET_KEY
    assert settings.yandex_search_folder_id == SECRET_FOLDER
    # Safe status must not echo the key — only say whether it's set and
    # a short masked preview at most.
    status = settings.safe_status()
    assert "search_api_key" in status
    assert "search_folder_id" in status
    assert status["search_configured"] is True
    assert status["search_api_key"] != SECRET_KEY
    assert status["search_folder_id"] != SECRET_FOLDER
    # Whatever shape, the full key must not appear.
    assert SECRET_KEY not in str(status)


def test_openapi_documents_wordstat_error_responses_and_legacy_deprecation():
    schema = TestClient(app).get("/openapi.json").json()
    for path in (
        "/wordstat/top",
        "/wordstat/dynamics",
        "/wordstat/regions",
        "/wordstat/regions-tree",
    ):
        responses = schema["paths"][path]["get"]["responses"]
        assert "200" in responses
        assert "502" in responses
        assert "503" in responses

    legacy_create = schema["paths"]["/yandex/keywords-research/wordstat/create"]["get"]
    assert legacy_create["deprecated"] is True
    assert "502" in legacy_create["responses"]

    legacy_get = schema["paths"]["/yandex/keywords-research/wordstat/{report_id}"]["get"]
    assert legacy_get["deprecated"] is True
    assert "502" in legacy_get["responses"]

    legacy_delete = schema["paths"]["/yandex/keywords-research/wordstat/{report_id}"]["delete"]
    assert legacy_delete["deprecated"] is True
    assert "502" in legacy_delete["responses"]
