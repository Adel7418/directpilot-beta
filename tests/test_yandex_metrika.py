"""Tests for the read-only Yandex Metrika client and /metrika/* endpoints.

All tests use httpx.MockTransport — no real network calls. The goal of
this module is to lock down:

- YandexMetrikaClient uses the Management API
  (https://api-metrika.yandex.net/management/v1) for counters / goals and
  the Stats API (https://api-metrika.yandex.net/stat/v1) for data:
  - GET /management/v1/counters
  - GET /management/v1/counter/{counter_id}/goals
  - GET /stat/v1/data
- Auth header is `Authorization: OAuth <token>` (the documented Metrika
  way) — not Api-Key, not Bearer.
- The goals-conversion metric for a Metrika counter is `ym:s:anyGoalReaches`
  (NOT `ym:s:goalReaches` — the latter is per-goal and was renamed).
- The traffic-source dimension is `ym:s:lastsignTrafficSource` (NOT the
  older `ym:s:TrafficSource` which is deprecated and breaks in v2).
- Missing YANDEX_METRIKA_OAUTH_TOKEN → YandexMetrikaError before any
  network call; the token is never echoed.
- HTTP error / transport error → YandexMetrikaError with the HTTP status
  only; the body is never echoed.
- FastAPI /metrika/* endpoints:
  - 503 when the token is missing.
  - 502 on upstream error with redacted message.
  - 200 with envelope ``{source: "yandex_metrika", read_only: true, data: ...}``
    on success.
  - Token is never echoed in the response body.
- Settings expose yandex_metrika_oauth_token; safe_status masks it; the
  status adds a ``metrika_configured`` flag.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.yandex_metrika import (
    YandexMetrikaClient,
    YandexMetrikaError,
    YandexMetrikaMissingTokenError,
)


SECRET_TOKEN = "TEST-METRIKA-TOKEN"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"_env_file": None, "yandex_metrika_oauth_token": SECRET_TOKEN}
    base.update(overrides)
    return Settings(**base)


def _client(handler, **overrides: Any) -> YandexMetrikaClient:
    return YandexMetrikaClient(settings=_settings(**overrides), transport=httpx.MockTransport(handler))


def _capture(status: int = 200, body: Any = None) -> tuple[dict, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["params"] = dict(request.url.params)
        captured["body"] = json.loads(request.content.decode()) if request.content else None
        return httpx.Response(status, json=body if body is not None else {})

    return captured, handler


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_expose_metrika_oauth_token_and_masked_status():
    settings = _settings()
    assert settings.yandex_metrika_oauth_token == SECRET_TOKEN
    status = settings.safe_status()
    # The metrika flag is exposed in safe_status, the value is masked, the
    # full token is never present.
    assert "metrika_configured" in status
    assert status["metrika_configured"] is True
    assert "metrika_key_status" in status
    assert status["metrika_key_status"] != SECRET_TOKEN
    assert SECRET_TOKEN not in str(status)


def test_settings_metrika_not_configured_when_token_absent():
    settings = Settings(_env_file=None)
    assert settings.is_metrika_configured is False
    status = settings.safe_status()
    assert status["metrika_configured"] is False


# ---------------------------------------------------------------------------
# Construction / auth
# ---------------------------------------------------------------------------


def test_metrika_client_uses_documented_base_urls():
    assert YandexMetrikaClient.MANAGEMENT_BASE_URL == "https://api-metrika.yandex.net/management/v1"
    assert YandexMetrikaClient.STATS_BASE_URL == "https://api-metrika.yandex.net/stat/v1"


def test_missing_token_raises_before_network():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        raise AssertionError("network must not be reached without a Metrika OAUTH token")

    client = _client(handler, yandex_metrika_oauth_token=None)
    with pytest.raises(YandexMetrikaMissingTokenError) as exc:
        client.list_counters()

    assert "YANDEX_METRIKA_OAUTH_TOKEN" in str(exc.value)
    assert SECRET_TOKEN not in str(exc.value)


# ---------------------------------------------------------------------------
# list_counters
# ---------------------------------------------------------------------------


def test_list_counters_gets_management_v1_counters_with_oauth_header():
    captured, handler = _capture(body={"counters": [{"id": 1, "name": "site", "site": "example.com"}]})
    client = _client(handler)

    result = client.list_counters()

    assert captured["method"] == "GET"
    assert captured["url"] == "https://api-metrika.yandex.net/management/v1/counters"
    assert captured["headers"]["authorization"] == f"OAuth {SECRET_TOKEN}"
    assert result["ok"] is True
    assert result["data"]["counters"][0]["id"] == 1
    assert SECRET_TOKEN not in str(result)


# ---------------------------------------------------------------------------
# goals
# ---------------------------------------------------------------------------


def test_goals_uses_management_v1_counter_goals_endpoint():
    captured, handler = _capture(body={"goals": [{"id": 7, "name": "purchase", "type": "action"}]})
    client = _client(handler)

    result = client.goals(counter_id=42)

    assert captured["method"] == "GET"
    assert (
        captured["url"]
        == "https://api-metrika.yandex.net/management/v1/counter/42/goals"
    )
    assert captured["headers"]["authorization"] == f"OAuth {SECRET_TOKEN}"
    assert result["ok"] is True
    assert result["data"]["goals"][0]["name"] == "purchase"
    assert SECRET_TOKEN not in str(result)


# ---------------------------------------------------------------------------
# summary (any-goal reaches + sessions)
# ---------------------------------------------------------------------------


def test_summary_uses_any_goal_reaches_metric_not_goal_reaches():
    """The whole point of the Metrika client: the goals-conversion metric
    is `ym:s:anyGoalReaches` (NOT `ym:s:goalReaches` which is per-goal and
    is no longer a valid metric in v2)."""
    captured, handler = _capture(
        body={
            "data": [
                {
                    "dimensions": [{"name": "ym:s:date", "id": "2026-01-01"}],
                    "metrics": [1234, 5678],
                }
            ]
        }
    )
    client = _client(handler)

    result = client.summary(counter_id=42, date1="2026-01-01", date2="2026-01-31")

    assert captured["method"] == "GET"
    assert captured["url"].startswith("https://api-metrika.yandex.net/stat/v1/data")
    assert captured["headers"]["authorization"] == f"OAuth {SECRET_TOKEN}"
    assert captured["body"] is None
    params = captured["params"]
    # The METRIC must be the documented goals-conversion metric, not the
    # per-goal one or the deprecated one.
    assert params["metrics"] == "ym:s:anyGoalReaches"
    assert params["metrics"] != "ym:s:goalReaches"
    # The required dimension for a date-range summary.
    assert params["dimensions"] == "ym:s:date"
    # Date range is forwarded.
    assert params["date1"] == "2026-01-01"
    assert params["date2"] == "2026-01-31"
    assert params["ids"] == "42"
    assert result["ok"] is True
    assert result["data"]["data"][0]["metrics"][0] == 1234
    assert SECRET_TOKEN not in str(result)


# ---------------------------------------------------------------------------
# traffic_sources (last-sign traffic source dimension)
# ---------------------------------------------------------------------------


def test_traffic_sources_uses_lastsign_traffic_source_dimension():
    """The whole point of the traffic_sources method: the dimension is
    `ym:s:lastsignTrafficSource` (NOT the older `ym:s:TrafficSource` which
    is deprecated and returns nothing useful for last-click attribution
    in v2)."""
    captured, handler = _capture(
        body={
            "data": [
                {
                    "dimensions": [{"name": "ym:s:lastsignTrafficSource", "id": "organic"}],
                    "metrics": [900],
                }
            ]
        }
    )
    client = _client(handler)

    result = client.traffic_sources(
        counter_id=42, date1="2026-01-01", date2="2026-01-31", limit=10
    )

    params = captured["params"]
    assert captured["method"] == "GET"
    assert captured["url"].startswith("https://api-metrika.yandex.net/stat/v1/data")
    assert captured["body"] is None
    # The DIMENSION must be the documented last-sign traffic source, not
    # the older capitalized one.
    assert params["dimensions"] == "ym:s:lastsignTrafficSource"
    assert params["dimensions"] != "ym:s:TrafficSource"
    assert params["metrics"] == "ym:s:visits"
    assert params["date1"] == "2026-01-01"
    assert params["date2"] == "2026-01-31"
    assert params["ids"] == "42"
    assert params["limit"] == "10"
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_http_error_redacts_token_and_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": f"unauthorized: OAuth {SECRET_TOKEN} is invalid"},
        )

    client = _client(handler)

    with pytest.raises(YandexMetrikaError) as exc:
        client.list_counters()

    message = str(exc.value)
    assert SECRET_TOKEN not in message
    assert "unauthorized" not in message
    assert "401" in message


def test_transport_error_redacts_token():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client(handler)

    with pytest.raises(YandexMetrikaError) as exc:
        client.list_counters()

    message = str(exc.value)
    assert SECRET_TOKEN not in message
    assert "ConnectError" in message or "transport" in message.lower()


# ---------------------------------------------------------------------------
# FastAPI /metrika/* endpoints
# ---------------------------------------------------------------------------


def _override_with_client(client_obj) -> None:
    from app import main as main_mod

    app.dependency_overrides[main_mod.get_settings] = lambda: _settings()
    app.dependency_overrides[main_mod.get_yandex_metrika_client] = lambda: client_obj


def _clear_overrides() -> None:
    from app import main as main_mod

    app.dependency_overrides.pop(main_mod.get_settings, None)
    app.dependency_overrides.pop(main_mod.get_yandex_metrika_client, None)


def test_counters_endpoint_returns_source_yandex_metrika_and_read_only():
    captured, handler = _capture(body={"counters": [{"id": 99, "name": "main"}]})
    client_obj = YandexMetrikaClient(
        settings=_settings(), transport=httpx.MockTransport(handler)
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get("/metrika/counters")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex_metrika"
    assert body["read_only"] is True
    assert body["data"]["counters"][0]["id"] == 99
    assert captured["url"] == "https://api-metrika.yandex.net/management/v1/counters"
    assert SECRET_TOKEN not in response.text


def test_goals_endpoint_forwards_counter_id_in_path():
    captured, handler = _capture(body={"goals": [{"id": 1, "name": "lead"}]})
    client_obj = YandexMetrikaClient(
        settings=_settings(), transport=httpx.MockTransport(handler)
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get("/metrika/counters/42/goals")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex_metrika"
    assert body["counter_id"] == 42
    assert captured["url"].endswith("/management/v1/counter/42/goals")


def test_summary_endpoint_forwards_date_range_and_uses_any_goal_reaches():
    captured, handler = _capture(body={"data": [{"metrics": [100]}]})
    client_obj = YandexMetrikaClient(
        settings=_settings(), transport=httpx.MockTransport(handler)
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get(
            "/metrika/counters/42/summary?date1=2026-01-01&date2=2026-01-31"
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    assert captured["body"] is None
    params = captured["params"]
    # The METRIC used by the summary endpoint must be the documented
    # goals-conversion metric, not the per-goal one.
    assert params["metrics"] == "ym:s:anyGoalReaches"
    assert params["metrics"] != "ym:s:goalReaches"
    assert params["dimensions"] == "ym:s:date"
    assert params["date1"] == "2026-01-01"
    assert params["date2"] == "2026-01-31"
    assert params["ids"] == "42"


def test_traffic_sources_endpoint_uses_lastsign_traffic_source_dimension():
    captured, handler = _capture(body={"data": [{"metrics": [50]}]})
    client_obj = YandexMetrikaClient(
        settings=_settings(), transport=httpx.MockTransport(handler)
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get(
            "/metrika/counters/42/traffic-sources"
            "?date1=2026-01-01&date2=2026-01-31&limit=10"
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    assert captured["body"] is None
    params = captured["params"]
    # The DIMENSION used by the traffic-sources endpoint must be the
    # documented last-sign traffic source, not the older one.
    assert params["dimensions"] == "ym:s:lastsignTrafficSource"
    assert params["dimensions"] != "ym:s:TrafficSource"
    assert params["limit"] == "10"


def test_metrika_endpoints_return_503_when_token_missing():
    """Without YANDEX_METRIKA_OAUTH_TOKEN, every /metrika/* endpoint must
    short-circuit with a 503 (service not configured) — never a 500
    crash and never an actual HTTP call."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        calls.append(request)
        return httpx.Response(200, json={})

    client_obj = YandexMetrikaClient(
        settings=Settings(_env_file=None, yandex_metrika_oauth_token=None),
        transport=httpx.MockTransport(handler),
    )
    _override_with_client(client_obj)
    try:
        api = TestClient(app)
        counters = api.get("/metrika/counters")
        goals = api.get("/metrika/counters/1/goals")
        summary = api.get("/metrika/counters/1/summary?date1=2026-01-01&date2=2026-01-31")
        traffic = api.get(
            "/metrika/counters/1/traffic-sources?date1=2026-01-01&date2=2026-01-31"
        )
    finally:
        _clear_overrides()

    assert calls == []
    for resp in (counters, goals, summary, traffic):
        assert resp.status_code == 503, resp.text
        detail = resp.json()["detail"]
        assert detail["error_type"] == "YandexMetrikaError"
        assert "YANDEX_METRIKA_OAUTH_TOKEN" in detail["message"]
        assert SECRET_TOKEN not in resp.text


def test_metrika_endpoints_return_502_on_upstream_error_with_redaction():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"message": f"server failure: OAuth {SECRET_TOKEN}"},
        )

    client_obj = YandexMetrikaClient(
        settings=_settings(), transport=httpx.MockTransport(handler)
    )
    _override_with_client(client_obj)
    try:
        response = TestClient(app).get("/metrika/counters")
    finally:
        _clear_overrides()

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error_type"] == "YandexMetrikaError"
    assert SECRET_TOKEN not in response.text
    assert "server failure" not in response.text


def test_metrika_openapi_documents_error_responses():
    schema = TestClient(app).get("/openapi.json").json()
    for path in (
        "/metrika/counters",
        "/metrika/counters/{counter_id}/goals",
        "/metrika/counters/{counter_id}/summary",
        "/metrika/counters/{counter_id}/traffic-sources",
    ):
        responses = schema["paths"][path]["get"]["responses"]
        assert "200" in responses
        assert "502" in responses
        assert "503" in responses
