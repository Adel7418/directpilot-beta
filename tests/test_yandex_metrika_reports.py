"""Behavior-first coverage for typed, server-owned Metrika report presets.

Every request uses ``httpx.MockTransport``. No live provider is called.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.yandex_metrika import YandexMetrikaClient


TEST_TOKEN = "test-metrika-token"
CORE_METRICS = (
    "ym:s:visits,ym:s:users,ym:s:pageviews,"
    "ym:s:anyGoalReaches,ym:s:anyGoalConversionRate"
)


def _settings(*, token: str | None = TEST_TOKEN) -> Settings:
    return Settings(
        _env_file=None,
        directpilot_mode="live_readonly",
        yandex_metrika_oauth_token=token,
    )


def _capture(*, body: Any | None = None, status: int = 200) -> tuple[dict[str, Any], Any]:
    captured: dict[str, Any] = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(status, json={} if body is None else body)

    return captured, handler


def _override_client(client: YandexMetrikaClient) -> None:
    from app import main as main_mod

    app.dependency_overrides[main_mod.get_settings] = _settings
    app.dependency_overrides[main_mod.get_yandex_metrika_client] = lambda: client


def _clear_overrides() -> None:
    from app import main as main_mod

    app.dependency_overrides.pop(main_mod.get_settings, None)
    app.dependency_overrides.pop(main_mod.get_yandex_metrika_client, None)


def _report_payload(*, dimensions: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "data": [
            {
                "dimensions": dimensions
                or [
                    {"id": "campaign-1", "name": "Campaign One"},
                    {"id": "group-2", "name": "Group Two"},
                    {"id": "ad-3", "name": "Ad Three"},
                ],
                "metrics": [12, 9, 20, None, 33.3],
            }
        ],
        "totals": [12, 9, 20, None, 33.3],
        "total_rows": 1,
    }


@pytest.mark.parametrize(
    ("preset", "path", "dimensions", "expects_limit"),
    [
        ("site-summary", "site-summary", "ym:s:date", False),
        (
            "direct-hierarchy",
            "direct-hierarchy",
            (
                "ym:s:lastsignDirectClickOrder,"
                "ym:s:lastsignDirectBannerGroup,"
                "ym:s:lastsignDirectClickBanner"
            ),
            True,
        ),
        (
            "utm-hierarchy",
            "utm-hierarchy",
            "ym:s:lastsignUTMSource,ym:s:lastsignUTMMedium,ym:s:lastsignUTMCampaign",
            True,
        ),
        ("landing-pages", "landing-pages", "ym:s:startURL", True),
    ],
)
def test_report_routes_send_only_their_exact_server_owned_query_params(
    preset: str, path: str, dimensions: str, expects_limit: bool
) -> None:
    captured, handler = _capture(body=_report_payload())
    _override_client(YandexMetrikaClient(settings=_settings(), transport=httpx.MockTransport(handler)))
    try:
        response = TestClient(app).get(
            f"/metrika/counters/42/reports/{path}"
            "?date1=2026-01-02&date2=2026-01-03&accuracy=full&limit=17"
            "&dimensions=attacker&metrics=ym:s:purchaseRevenue&filters=attacker"
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    assert captured["calls"] == 1
    assert captured["method"] == "GET"
    assert captured["url"].startswith("https://api-metrika.yandex.net/stat/v1/data")
    assert captured["params"]["ids"] == "42"
    assert captured["params"]["date1"] == "2026-01-02"
    assert captured["params"]["date2"] == "2026-01-03"
    assert captured["params"]["accuracy"] == "full"
    assert captured["params"]["dimensions"] == dimensions
    assert captured["params"]["metrics"] == CORE_METRICS
    assert "ym:s:purchaseRevenue" not in captured["params"]["metrics"]
    assert "filters" not in captured["params"]
    assert "Authorization" not in captured["params"]
    if expects_limit:
        assert captured["params"]["limit"] == "17"
        assert captured["params"]["sort"] == "-ym:s:visits"
    else:
        assert "limit" not in captured["params"]
        assert "sort" not in captured["params"]


def test_catalog_exposes_only_the_documented_preset_identifiers() -> None:
    response = TestClient(app).get("/metrika/reports/catalog")

    assert response.status_code == 200, response.text
    body = response.json()
    presets = {item["preset"]: item for item in body["presets"]}
    assert set(presets) == {
        "site-summary",
        "direct-hierarchy",
        "utm-hierarchy",
        "landing-pages",
        "traffic-sources",
    }
    assert presets["site-summary"]["dimensions"] == ["ym:s:date"]
    assert presets["direct-hierarchy"]["dimensions"] == [
        "ym:s:lastsignDirectClickOrder",
        "ym:s:lastsignDirectBannerGroup",
        "ym:s:lastsignDirectClickBanner",
    ]
    assert presets["utm-hierarchy"]["dimensions"] == [
        "ym:s:lastsignUTMSource",
        "ym:s:lastsignUTMMedium",
        "ym:s:lastsignUTMCampaign",
    ]
    assert presets["landing-pages"]["dimensions"] == ["ym:s:startURL"]
    assert presets["site-summary"]["core_metrics"] == CORE_METRICS.split(",")
    assert all(item["supports_ecommerce"] is False for item in presets.values())
    assert all("ecommerce_metric_template" not in item for item in presets.values())
    assert body["source"] == "yandex"
    assert body["read_only"] is True


def test_report_defaults_to_yesterday_completed_day() -> None:
    captured, handler = _capture(body=_report_payload())
    _override_client(YandexMetrikaClient(settings=_settings(), transport=httpx.MockTransport(handler)))
    try:
        response = TestClient(app).get("/metrika/counters/42/reports/direct-hierarchy")
    finally:
        _clear_overrides()

    expected = str(date.today() - timedelta(days=1))
    assert response.status_code == 200, response.text
    assert captured["params"]["date1"] == expected
    assert captured["params"]["date2"] == expected
    assert response.json()["period"] == {
        "date1": expected,
        "date2": expected,
        "completed_day": True,
    }


@pytest.mark.parametrize(
    "suffix",
    [
        "?date1=2026-01-01",
        "?date2=2026-01-01",
        f"?date1={date.today() + timedelta(days=1)}&date2={date.today() + timedelta(days=1)}",
        "?date1=2026-02-01&date2=2026-01-01",
    ],
)
def test_report_date_contract_rejects_invalid_ranges_before_transport(suffix: str) -> None:
    captured, handler = _capture(body=_report_payload())
    _override_client(YandexMetrikaClient(settings=_settings(), transport=httpx.MockTransport(handler)))
    try:
        response = TestClient(app).get(f"/metrika/counters/42/reports/direct-hierarchy{suffix}")
    finally:
        _clear_overrides()

    assert response.status_code == 422, response.text
    assert captured["calls"] == 0


@pytest.mark.parametrize(
    "suffix",
    [
        "?date1=2026-01-01&date2=2026-01-02&limit=0",
        "?date1=2026-01-01&date2=2026-01-02&limit=1001",
        "?date1=2026-01-01&date2=2026-01-02&accuracy=unsafe",
        "?date1=2026-01-01&date2=2026-01-02&view=custom",
        "?date1=2026-01-01&date2=2026-01-02&view=ecommerce&currency=GBP",
    ],
)
def test_report_option_contract_rejects_invalid_values_before_transport(suffix: str) -> None:
    captured, handler = _capture(body=_report_payload())
    _override_client(YandexMetrikaClient(settings=_settings(), transport=httpx.MockTransport(handler)))
    try:
        response = TestClient(app).get(f"/metrika/counters/42/reports/direct-hierarchy{suffix}")
    finally:
        _clear_overrides()

    assert response.status_code == 422, response.text
    assert captured["calls"] == 0


@pytest.mark.parametrize(
    "path",
    ["site-summary", "direct-hierarchy", "utm-hierarchy", "landing-pages"],
)
def test_ecommerce_view_is_rejected_before_provider_for_every_report_route(path: str) -> None:
    captured, handler = _capture(body=_report_payload())
    _override_client(YandexMetrikaClient(settings=_settings(), transport=httpx.MockTransport(handler)))
    try:
        response = TestClient(app).get(
            f"/metrika/counters/42/reports/{path}"
            "?date1=2026-01-01&date2=2026-01-01&view=ecommerce"
        )
    finally:
        _clear_overrides()

    assert response.status_code == 422, response.text
    assert captured["calls"] == 0


def test_report_normalizes_rows_and_surfaces_provider_metadata() -> None:
    payload = _report_payload()
    payload.update(
        {
            "sampled": True,
            "sample_share": 0.25,
            "sample_size": 250,
            "sample_space": 1000,
            "data_lag": 90,
            "contains_sensitive_data": True,
            "total_rows_rounded": True,
        }
    )
    captured, handler = _capture(body=payload)
    _override_client(YandexMetrikaClient(settings=_settings(), transport=httpx.MockTransport(handler)))
    try:
        response = TestClient(app).get(
            "/metrika/counters/42/reports/direct-hierarchy"
            "?date1=2026-01-01&date2=2026-01-01"
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"] == "direct-hierarchy"
    assert body["counter_id"] == 42
    assert body["period"] == {"date1": "2026-01-01", "date2": "2026-01-01", "completed_day": True}
    assert body["dimensions"] == [
        "ym:s:lastsignDirectClickOrder",
        "ym:s:lastsignDirectBannerGroup",
        "ym:s:lastsignDirectClickBanner",
    ]
    assert body["metrics"] == CORE_METRICS.split(",")
    assert body["items"] == [
        {
            "dimensions": [
                {"id": "campaign-1", "name": "Campaign One"},
                {"id": "group-2", "name": "Group Two"},
                {"id": "ad-3", "name": "Ad Three"},
            ],
            "metrics": {
                "ym:s:visits": 12.0,
                "ym:s:users": 9.0,
                "ym:s:pageviews": 20.0,
                "ym:s:anyGoalReaches": None,
                "ym:s:anyGoalConversionRate": 33.3,
            },
        }
    ]
    assert body["totals"] == {
        "ym:s:visits": 12.0,
        "ym:s:users": 9.0,
        "ym:s:pageviews": 20.0,
        "ym:s:anyGoalReaches": None,
        "ym:s:anyGoalConversionRate": 33.3,
    }
    assert body["row_count"] == 1
    assert body["sampled"] is True
    assert body["sample_share"] == 0.25
    assert body["sample_size"] == 250
    assert body["sample_space"] == 1000
    assert body["data_lag"] == 90
    assert body["contains_sensitive_data"] is True
    assert body["total_rows_rounded"] is True
    assert body["source"] == "yandex"
    assert body["read_only"] is True


def test_empty_provider_data_is_a_valid_empty_report() -> None:
    captured, handler = _capture(body={"data": [], "totals": []})
    _override_client(YandexMetrikaClient(settings=_settings(), transport=httpx.MockTransport(handler)))
    try:
        response = TestClient(app).get(
            "/metrika/counters/42/reports/landing-pages"
            "?date1=2026-01-01&date2=2026-01-01"
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["totals"] is None
    assert body["row_count"] == 0


def test_report_missing_token_maps_to_503_without_provider_call() -> None:
    captured, handler = _capture(body=_report_payload())
    _override_client(
        YandexMetrikaClient(
            settings=_settings(token=None), transport=httpx.MockTransport(handler)
        )
    )
    try:
        response = TestClient(app).get("/metrika/counters/42/reports/direct-hierarchy")
    finally:
        _clear_overrides()

    assert response.status_code == 503, response.text
    assert captured["calls"] == 0
    assert TEST_TOKEN not in response.text


def test_report_provider_error_maps_to_sanitized_502() -> None:
    captured, handler = _capture(
        status=500,
        body={"message": "provider detail must never be surfaced"},
    )
    _override_client(YandexMetrikaClient(settings=_settings(), transport=httpx.MockTransport(handler)))
    try:
        response = TestClient(app).get("/metrika/counters/42/reports/direct-hierarchy")
    finally:
        _clear_overrides()

    assert response.status_code == 502, response.text
    assert captured["calls"] == 1
    assert "provider detail" not in response.text
    assert TEST_TOKEN not in response.text


def test_legacy_summary_and_traffic_sources_preserve_exact_envelopes_and_provider_params() -> None:
    payload = _report_payload(dimensions=[{"id": "organic", "name": "Organic"}])
    captured, handler = _capture(body=payload)
    _override_client(YandexMetrikaClient(settings=_settings(), transport=httpx.MockTransport(handler)))
    try:
        api = TestClient(app)
        summary = api.get("/metrika/counters/42/summary?date1=2026-01-01&date2=2026-01-01")
        summary_params = dict(captured["params"])
        traffic = api.get(
            "/metrika/counters/42/traffic-sources?date1=2026-01-01&date2=2026-01-01"
        )
        traffic_params = dict(captured["params"])
    finally:
        _clear_overrides()

    assert summary.status_code == 200, summary.text
    assert traffic.status_code == 200, traffic.text
    assert summary_params == {
        "ids": "42",
        "date1": "2026-01-01",
        "date2": "2026-01-01",
        "accuracy": "high",
        "dimensions": "ym:s:date",
        "metrics": CORE_METRICS,
    }
    assert traffic_params == {
        "ids": "42",
        "date1": "2026-01-01",
        "date2": "2026-01-01",
        "accuracy": "high",
        "dimensions": "ym:s:lastsignTrafficSource",
        "metrics": CORE_METRICS,
        "sort": "-ym:s:visits",
        "limit": "10",
    }
    for response, method in ((summary, "summary"), (traffic, "traffic_sources")):
        assert response.json() == {
            "service": "stat",
            "method": method,
            "counter_id": 42,
            "data": payload,
            "source": "yandex_metrika",
            "read_only": True,
        }


def test_legacy_metrika_defaults_to_yesterday_before_provider_calls() -> None:
    payload = _report_payload()
    captured, handler = _capture(body=payload)
    _override_client(YandexMetrikaClient(settings=_settings(), transport=httpx.MockTransport(handler)))
    try:
        api = TestClient(app)
        summary = api.get("/metrika/counters/42/summary")
        summary_params = dict(captured["params"])
        traffic = api.get("/metrika/counters/42/traffic-sources")
        traffic_params = dict(captured["params"])
    finally:
        _clear_overrides()

    expected = str(date.today() - timedelta(days=1))
    assert summary.status_code == 200, summary.text
    assert traffic.status_code == 200, traffic.text
    assert summary_params["date1"] == expected
    assert summary_params["date2"] == expected
    assert traffic_params["date1"] == expected
    assert traffic_params["date2"] == expected


def test_legacy_metrika_routes_mock_without_a_token_or_provider_calls() -> None:
    settings = Settings(_env_file=None, directpilot_mode="mock", yandex_metrika_oauth_token=None)
    captured, handler = _capture()
    client = YandexMetrikaClient(settings=settings, transport=httpx.MockTransport(handler))
    from app import main as main_mod

    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_metrika_client] = lambda: client
    try:
        api = TestClient(app)
        summary = api.get("/metrika/counters/42/summary")
        traffic = api.get("/metrika/counters/42/traffic-sources")
    finally:
        _clear_overrides()

    assert captured["calls"] == 0
    for response, method in ((summary, "summary"), (traffic, "traffic_sources")):
        assert response.status_code == 200, response.text
        assert response.json() == {
            "service": "stat",
            "method": method,
            "counter_id": 42,
            "data": {"data": [], "totals": []},
            "source": "yandex_metrika",
            "read_only": True,
        }


def test_legacy_metrika_openapi_keeps_envelope_model_and_parameters() -> None:
    schema = TestClient(app).get("/openapi.json").json()
    expected_parameters = {
        "/metrika/counters/{counter_id}/summary": {"counter_id", "date1", "date2"},
        "/metrika/counters/{counter_id}/traffic-sources": {"counter_id", "date1", "date2", "limit"},
    }

    for path, parameters in expected_parameters.items():
        operation = schema["paths"][path]["get"]
        response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert response_schema == {"$ref": "#/components/schemas/YandexMetrikaResult"}
        assert {parameter["name"] for parameter in operation["parameters"]} == parameters


def test_reports_openapi_includes_catalog_and_all_public_report_routes() -> None:
    schema = TestClient(app).get("/openapi.json").json()
    catalog_responses = schema["paths"]["/metrika/reports/catalog"]["get"]["responses"]
    assert "200" in catalog_responses
    for path in (
        "/metrika/counters/{counter_id}/reports/site-summary",
        "/metrika/counters/{counter_id}/reports/direct-hierarchy",
        "/metrika/counters/{counter_id}/reports/utm-hierarchy",
        "/metrika/counters/{counter_id}/reports/landing-pages",
    ):
        responses = schema["paths"][path]["get"]["responses"]
        assert "200" in responses
        assert "422" in responses
        assert "502" in responses
        assert "503" in responses
