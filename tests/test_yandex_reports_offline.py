from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.yandex_direct import YandexDirectClient
from tests.report_test_support import (
    make_direct_client,
    report_overrides,
    report_tsv,
    settings_for,
)


OFFICIAL_REPORT_TYPES = [
    "ACCOUNT_PERFORMANCE_REPORT",
    "CAMPAIGN_PERFORMANCE_REPORT",
    "ADGROUP_PERFORMANCE_REPORT",
    "AD_PERFORMANCE_REPORT",
    "CRITERIA_PERFORMANCE_REPORT",
    "CUSTOM_REPORT",
    "REACH_AND_FREQUENCY_PERFORMANCE_REPORT",
    "SEARCH_QUERY_PERFORMANCE_REPORT",
]


@pytest.mark.parametrize("status_code,retry_in,expected_retry", [(201, "7", 7), (202, "99999", 300)])
def test_offline_reports_return_bounded_pending_instead_of_tsv(
    status_code: int,
    retry_in: str,
    expected_retry: int,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content="not-a-completed-tsv", headers={"retryIn": retry_in})

    client = make_direct_client(settings_for(), handler)
    result = client.report(
        "SEARCH_QUERY_PERFORMANCE_REPORT",
        date_from="2026-08-01",
        date_to="2026-08-01",
    )

    assert result["ok"] is True
    assert result["status"] == "pending"
    assert result["retry_after_seconds"] == expected_retry
    assert "result" not in result


def test_provider_json_error_with_200_never_becomes_empty_tsv_or_leaks_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 53, "error_detail": "unit-test-token must stay private"}},
        )

    settings = settings_for()
    direct_client = make_direct_client(settings, handler)
    with report_overrides(settings, direct_client) as api:
        response = api.get("/yandex/reports/campaign-performance")

    assert response.status_code == 502, response.text
    assert response.json()["detail"]["error_type"] == "YandexDirectError"
    assert "unit-test-token" not in response.text


@pytest.mark.parametrize("report_type", OFFICIAL_REPORT_TYPES)
def test_raw_report_diagnostics_allow_only_each_official_preset(report_type: str):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        fields = captured["payload"]["params"]["FieldNames"]
        return httpx.Response(200, content=report_tsv(fields))

    settings = settings_for()
    direct_client = make_direct_client(settings, handler)
    with report_overrides(settings, direct_client) as api:
        response = api.get(
            f"/yandex/reports/live/{report_type}",
            params={"date_from": "2026-08-01", "date_to": "2026-08-01"},
        )

    assert response.status_code == 200, response.text
    assert captured["payload"]["params"]["ReportType"] == report_type
    assert captured["payload"]["params"]["FieldNames"]
    assert response.json()["read_only"] is True


def test_raw_report_rejects_unknown_type_and_incompatible_view_before_transport():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content="unexpected")

    settings = settings_for()
    direct_client = make_direct_client(settings, handler)
    with report_overrides(settings, direct_client) as api:
        unknown = api.get(
            "/yandex/reports/live/NOT_A_DIRECT_REPORT",
            params={"date_from": "2026-08-01", "date_to": "2026-08-01"},
        )
        incompatible = api.get(
            "/yandex/reports/live/REACH_AND_FREQUENCY_PERFORMANCE_REPORT",
            params={"date_from": "2026-08-01", "date_to": "2026-08-01", "view": "positions"},
        )

    assert [response.status_code for response in (unknown, incompatible)] == [422, 422]
    assert calls == []


def test_search_query_raw_diagnostic_uses_the_same_validated_preset_and_pending_contract():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(201, headers={"retryIn": "3"})

    settings = settings_for()
    direct_client = make_direct_client(settings, handler)
    with report_overrides(settings, direct_client) as api:
        response = api.get(
            "/yandex/reports/search-queries-live",
            params={"date_from": "2026-08-01", "date_to": "2026-08-01"},
        )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "status": "pending",
        "retry_after_seconds": 3,
        "report_type": "SEARCH_QUERY_PERFORMANCE_REPORT",
        "surface": "search-queries",
        "read_only": True,
    }
    assert captured["payload"]["params"]["ReportType"] == "SEARCH_QUERY_PERFORMANCE_REPORT"
    assert "Query" in captured["payload"]["params"]["FieldNames"]


def test_typed_pending_response_does_not_parse_provider_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, content="not-tsv", headers={"retryIn": "4"})

    settings = settings_for()
    direct_client = make_direct_client(settings, handler)
    with report_overrides(settings, direct_client) as api:
        response = api.get("/yandex/reports/search-queries")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["retry_after_seconds"] == 4
    assert body["read_only"] is True


def test_report_http_error_is_redacted_at_typed_route_boundary():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="unit-test-token must stay private")

    settings = settings_for()
    direct_client = make_direct_client(settings, handler)
    with report_overrides(settings, direct_client) as api:
        response = api.get("/yandex/reports/ad-performance")

    assert response.status_code == 502, response.text
    assert "unit-test-token" not in response.text


def test_raw_live_report_openapi_documents_all_runtime_error_statuses():
    from app.main import app

    paths = app.openapi()["paths"]
    expected_statuses = {"202", "409", "422", "502"}

    for path in (
        "/yandex/reports/live/{report_type}",
        "/yandex/reports/search-queries-live",
    ):
        assert expected_statuses <= set(paths[path]["get"]["responses"])
