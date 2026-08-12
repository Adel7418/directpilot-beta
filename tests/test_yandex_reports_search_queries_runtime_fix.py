"""Focused regression coverage for the search-query runtime report fix.

All provider interactions use httpx.MockTransport; this module never performs
an external Yandex API call.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.config import Settings
from app.main import app
from app.yandex_direct import YandexDirectClient


SEARCH_FIELDS = [
    "Query",
    "CampaignId",
    "AdGroupId",
    "Impressions",
    "Clicks",
    "Ctr",
    "Cost",
]


def _settings() -> Settings:
    return Settings(_env_file=None, directpilot_mode="live_readonly", yandex_oauth_token="TEST-SECRET")


def _install(handler) -> tuple[TestClient, list[dict[str, Any]]]:
    settings = _settings()
    calls: list[dict[str, Any]] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "path": request.url.path,
                "json": json.loads(request.content.decode()) if request.content else None,
            }
        )
        return handler(request)

    yandex_client = YandexDirectClient(
        settings=settings,
        transport=httpx.MockTransport(recording_handler),
    )
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: yandex_client
    return TestClient(app), calls


def _clear_overrides() -> None:
    app.dependency_overrides.pop(main_mod.get_settings, None)
    app.dependency_overrides.pop(main_mod.get_yandex_client, None)


def _query_tsv(
    rows: list[list[str]] | None = None,
    *,
    header: list[str] | None = None,
    newline: str = "\n",
) -> str:
    header = header or SEARCH_FIELDS
    rows = rows if rows is not None else [
        ["ремонт кондиционера Казань", "710691939", "1001", "12", "2", "16.67", "2914.37"],
    ]
    return newline.join(["\t".join(header), *("\t".join(row) for row in rows)]) + newline


def test_runtime_fix_reports_http_error_is_allowlisted_and_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/reports")
        return httpx.Response(
            400,
            json={
                "error": {
                    "error_code": 4000,
                    "error_string": "Unsupported report field",
                    "error_detail": "Query is unavailable for this report",
                    "authorization": "Bearer TEST-SECRET",
                    "request_body": "RAW-BODY-MUST-NOT-LEAK",
                },
                "token": "TEST-SECRET",
                "raw_body": "RAW-BODY-MUST-NOT-LEAK",
            },
        )

    client, _ = _install(handler)
    try:
        response = client.get(
            "/yandex/reports/search-queries",
            params={"date_from": "2026-08-11", "date_to": "2026-08-11"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 502, response.text
    assert response.json()["detail"] == {
        "provider": "yandex_direct",
        "service": "reports",
        "method": "POST",
        "report_type": "SEARCH_QUERY_PERFORMANCE_REPORT",
        "http_status": 400,
        "error_code": 4000,
        "error_string": "Unsupported report field",
        "error_detail": "Query is unavailable for this report",
    }
    assert "TEST-SECRET" not in response.text
    assert "RAW-BODY-MUST-NOT-LEAK" not in response.text


def test_runtime_fix_reports_json_error_200_is_allowlisted_and_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error": {
                    "error_code": 52,
                    "error_string": "Reports request rejected",
                    "error_detail": "Safe provider detail",
                    "cookie": "session=TEST-SECRET",
                },
                "Authorization": "Bearer TEST-SECRET",
            },
        )

    client, _ = _install(handler)
    try:
        response = client.get("/yandex/reports/search-queries")
    finally:
        _clear_overrides()

    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert detail == {
        "provider": "yandex_direct",
        "service": "reports",
        "method": "POST",
        "report_type": "SEARCH_QUERY_PERFORMANCE_REPORT",
        "http_status": 200,
        "error_code": 52,
        "error_string": "Reports request rejected",
        "error_detail": "Safe provider detail",
    }
    assert "TEST-SECRET" not in response.text


def test_runtime_fix_header_driven_parser_preserves_unicode_and_normalizes_documented_numbers() -> None:
    header = ["Cost", "Query", "Clicks", "CampaignId", "Impressions", "AdGroupId", "Ctr", "Date"]
    row = ["2\u00a0914,37", "ремонт кондиционера Казань", "2", "710691939", "12", "1001", "16,67", "2026-08-11"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="\ufeff" + _query_tsv([row], header=header, newline="\r\n"))

    client, calls = _install(handler)
    try:
        response = client.get("/yandex/reports/search-queries")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert body["report_type"] == "SEARCH_QUERY_PERFORMANCE_REPORT"
    assert body["query_fields"] == SEARCH_FIELDS
    assert body["partial_failure"] is False
    assert body["warnings"] == []
    assert body["items"] == [
        {
            "date": "2026-08-11",
            "query": "ремонт кондиционера Казань",
            "campaign_id": "710691939",
            "campaign_name": None,
            "ad_group_id": "1001",
            "impressions": 12,
            "clicks": 2,
            "ctr": 16.67,
            "cost": 2914.37,
        }
    ]
    report_call = next(call for call in calls if call["path"].endswith("/reports"))
    params = report_call["json"]["params"]
    assert params["ReportType"] == "SEARCH_QUERY_PERFORMANCE_REPORT"
    assert params["FieldNames"] == SEARCH_FIELDS
    assert "Date" not in params["FieldNames"]
    assert "CampaignName" not in params["FieldNames"]
    assert params["SelectionCriteria"].get("Filter") is None


def test_runtime_fix_missing_query_header_is_a_safe_parse_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content="CampaignId\tImpressions\tClicks\tCost\n710691939\t12\t2\t2914.37\n",
        )

    client, _ = _install(handler)
    try:
        response = client.get("/yandex/reports/search-queries")
    finally:
        _clear_overrides()

    assert response.status_code == 502, response.text
    assert response.json()["detail"] == {
        "provider": "yandex_direct",
        "service": "reports",
        "method": "POST",
        "report_type": "SEARCH_QUERY_PERFORMANCE_REPORT",
        "error_code": "missing_query_column",
        "error_string": "Search query report parse contract violation",
        "error_detail": "Required Query column is missing",
    }


@pytest.mark.parametrize("blank_query", ["", "   "])
def test_runtime_fix_blank_query_value_is_a_safe_parse_502(blank_query: str) -> None:
    query_tsv = _query_tsv(
        [[blank_query, "710691939", "1001", "12", "2", "16.67", "2914.37"]]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json={"result": {"Campaigns": []}})
        report_type = json.loads(request.content.decode())["params"]["ReportType"]
        if report_type == "CAMPAIGN_PERFORMANCE_REPORT":
            return httpx.Response(200, content=_campaign_tsv(clicks="2", cost="2914.37"))
        return httpx.Response(200, content=query_tsv)

    client, _ = _install(handler)
    try:
        response = client.get("/yandex/reports/search-queries")
    finally:
        _clear_overrides()

    assert response.status_code == 502, response.text
    assert response.json()["detail"] == {
        "provider": "yandex_direct",
        "service": "reports",
        "method": "POST",
        "report_type": "SEARCH_QUERY_PERFORMANCE_REPORT",
        "error_code": "empty_query_value",
        "error_string": "Search query report parse contract violation",
        "error_detail": "Required Query value is empty",
    }
    assert "TEST-SECRET" not in response.text
    assert "CampaignId" not in response.text
    assert "710691939" not in response.text
    assert query_tsv not in response.text


def test_runtime_fix_empty_body_and_valid_header_only_remain_empty_yandex_results() -> None:
    responses = ["", _query_tsv([])]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json={"result": {"Campaigns": []}})
        report_type = json.loads(request.content.decode())["params"]["ReportType"]
        if report_type == "CAMPAIGN_PERFORMANCE_REPORT":
            return httpx.Response(200, content=_campaign_tsv(clicks="0", cost="0.00"))
        return httpx.Response(200, content=responses.pop(0))

    client, _ = _install(handler)
    try:
        for _ in range(2):
            response = client.get("/yandex/reports/search-queries")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["source"] == "yandex"
            assert body["items"] == []
            assert body["partial_failure"] is False
            assert body["reconciliation"]["status"] == "matched"
    finally:
        _clear_overrides()


def test_runtime_fix_empty_account_report_falls_back_per_campaign_merges_and_deduplicates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Campaigns": [
                            {"Id": "710691939", "Name": "Кондиционеры"},
                            {"Id": "713397771", "Name": "Стиральные машины"},
                        ]
                    }
                },
            )
        params = json.loads(request.content.decode())["params"]
        if params["ReportType"] == "CAMPAIGN_PERFORMANCE_REPORT":
            return httpx.Response(200, content=_campaign_tsv(clicks="3", cost="3137.21"))
        selection = params["SelectionCriteria"]
        if "Filter" not in selection:
            return httpx.Response(200, content=_query_tsv([]))
        campaign_id = selection["Filter"][0]["Values"][0]
        if campaign_id == "710691939":
            return httpx.Response(
                200,
                content=_query_tsv(
                    [
                        ["ремонт кондиционера", "710691939", "1001", "12", "2", "16.67", "2914.37"],
                        ["ремонт кондиционера", "710691939", "1001", "12", "2", "16.67", "2914.37"],
                    ]
                ),
            )
        return httpx.Response(
            200,
            content=_query_tsv(
                [["ремонт стиральной машины", "713397771", "2001", "8", "1", "12.5", "222.84"]]
            ),
        )

    client, calls = _install(handler)
    try:
        response = client.get("/yandex/reports/search-queries")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(item["campaign_id"], item["query"]) for item in body["items"]] == [
        ("710691939", "ремонт кондиционера"),
        ("713397771", "ремонт стиральной машины"),
    ]
    assert [item["campaign_name"] for item in body["items"]] == [
        "Кондиционеры",
        "Стиральные машины",
    ]
    assert body["partial_failure"] is False
    assert body["reconciliation"]["status"] == "matched"
    report_calls = [call for call in calls if call["path"].endswith("/reports")]
    assert len(report_calls) == 4
    assert [call["json"]["params"]["ReportType"] for call in report_calls] == [
        "SEARCH_QUERY_PERFORMANCE_REPORT",
        "SEARCH_QUERY_PERFORMANCE_REPORT",
        "SEARCH_QUERY_PERFORMANCE_REPORT",
        "CAMPAIGN_PERFORMANCE_REPORT",
    ]
    assert all(
        "CampaignIds" not in call["json"]["params"]["SelectionCriteria"]
        for call in report_calls
    )
    assert report_calls[1]["json"]["params"]["SelectionCriteria"]["Filter"] == [
        {"Field": "CampaignId", "Operator": "IN", "Values": ["710691939"]}
    ]


def test_runtime_fix_nonempty_account_report_and_explicit_campaign_do_not_fan_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/campaigns"):
            raise AssertionError("campaign fallback must not run")
        params = json.loads(request.content.decode())["params"]
        if params["ReportType"] == "CAMPAIGN_PERFORMANCE_REPORT":
            return httpx.Response(200, content=_campaign_tsv(clicks="2", cost="2914.37"))
        return httpx.Response(
            200,
            content=_query_tsv(
                [
                    [
                        "ремонт кондиционера",
                        "710691939",
                        "Кондиционеры",
                        "1001",
                        "12",
                        "2",
                        "16.67",
                        "2914.37",
                    ]
                ],
                header=["Query", "CampaignId", "CampaignName", "AdGroupId", "Impressions", "Clicks", "Ctr", "Cost"],
            ),
        )

    client, calls = _install(handler)
    try:
        account_response = client.get("/yandex/reports/search-queries")
        scoped_response = client.get("/yandex/reports/search-queries", params={"campaign_id": "710691939"})
    finally:
        _clear_overrides()

    assert account_response.status_code == 200, account_response.text
    assert scoped_response.status_code == 200, scoped_response.text
    assert account_response.json()["reconciliation"]["status"] == "matched"
    assert scoped_response.json()["reconciliation"]["status"] == "matched"
    report_calls = [call for call in calls if call["path"].endswith("/reports")]
    assert len(report_calls) == 4
    assert [call["json"]["params"]["ReportType"] for call in report_calls] == [
        "SEARCH_QUERY_PERFORMANCE_REPORT",
        "CAMPAIGN_PERFORMANCE_REPORT",
        "SEARCH_QUERY_PERFORMANCE_REPORT",
        "CAMPAIGN_PERFORMANCE_REPORT",
    ]
    assert report_calls[0]["json"]["params"]["SelectionCriteria"].get("Filter") is None
    expected_filter = [{"Field": "CampaignId", "Operator": "IN", "Values": ["710691939"]}]
    assert report_calls[2]["json"]["params"]["SelectionCriteria"]["Filter"] == expected_filter
    assert report_calls[3]["json"]["params"]["SelectionCriteria"]["Filter"] == expected_filter


def test_runtime_fix_per_campaign_provider_failure_is_visible_partial_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json={"result": {"Campaigns": [{"Id": "710691939", "Name": "Кондиционеры"}]}})
        params = json.loads(request.content.decode())["params"]
        if "Filter" not in params["SelectionCriteria"]:
            return httpx.Response(200, content=_query_tsv([]))
        return httpx.Response(
            400,
            json={
                "error": {
                    "error_code": 4000,
                    "error_string": "Rejected campaign report",
                    "error_detail": "Safe per-campaign detail",
                    "body": "TEST-SECRET",
                }
            },
        )

    yandex_client = YandexDirectClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    items, warnings, partial_failure = main_mod._fallback_search_query_reports(
        yandex_client,
        date_from="2026-08-11",
        date_to="2026-08-11",
    )

    assert items == []
    assert partial_failure is True
    assert warnings == [
        {
            "code": "search_query_fallback_campaign_failure",
            "campaign_id": "710691939",
            "provider_error": {
                "provider": "yandex_direct",
                "service": "reports",
                "method": "POST",
                "report_type": "SEARCH_QUERY_PERFORMANCE_REPORT",
                "http_status": 400,
                "error_code": 4000,
                "error_string": "Rejected campaign report",
                "error_detail": "Safe per-campaign detail",
            },
        }
    ]
    assert "TEST-SECRET" not in json.dumps(warnings)


def _campaign_tsv(*, clicks: str, cost: str, campaign_id: str = "710691939") -> str:
    return (
        "Date\tCampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\n"
        f"2026-08-11\t{campaign_id}\tCampaign\t100\t{clicks}\t{cost}\t0.00\n"
    )


def test_runtime_fix_filters_zero_clicks_before_ordered_pagination() -> None:
    query_rows = [
        ["zero-first", "710691939", "1001", "1", "0", "0.00", "0.00"],
        ["first-click", "710691939", "1001", "2", "1", "50.00", "10.00"],
        ["zero-middle", "710691939", "1001", "3", "0", "0.00", "0.00"],
        ["third-click", "710691939", "1001", "4", "2", "50.00", "20.00"],
        ["fourth-click", "710691939", "1001", "5", "3", "50.00", "30.00"],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json={"result": {"Campaigns": []}})
        report_type = json.loads(request.content.decode())["params"]["ReportType"]
        if report_type == "CAMPAIGN_PERFORMANCE_REPORT":
            return httpx.Response(200, content=_campaign_tsv(clicks="6", cost="60.00"))
        return httpx.Response(200, content=_query_tsv(query_rows))

    client, calls = _install(handler)
    try:
        response = client.get(
            "/yandex/reports/search-queries",
            params={"include_zero_clicks": "false", "limit": 2, "offset": 1},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_count"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [item["query"] for item in body["items"]] == ["third-click", "fourth-click"]
    report_types = [
        call["json"]["params"]["ReportType"]
        for call in calls
        if call["path"].endswith("/reports")
    ]
    assert report_types == ["SEARCH_QUERY_PERFORMANCE_REPORT", "CAMPAIGN_PERFORMANCE_REPORT"]


def test_runtime_fix_validates_pagination_bounds() -> None:
    client, calls = _install(lambda request: (_ for _ in ()).throw(AssertionError("must not call provider")))
    try:
        for params in ({"limit": 0}, {"limit": 5001}, {"offset": -1}):
            response = client.get("/yandex/reports/search-queries", params=params)
            assert response.status_code == 422, response.text
    finally:
        _clear_overrides()

    assert calls == []


def test_runtime_fix_reconciliation_exact_uses_the_explicit_campaign_scope() -> None:
    query_rows = [
        ["first", "710691939", "1001", "2", "2", "100.00", "10.00"],
        ["second", "710691939", "1001", "3", "3", "100.00", "12.34"],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json={"result": {"Campaigns": []}})
        report_type = json.loads(request.content.decode())["params"]["ReportType"]
        if report_type == "CAMPAIGN_PERFORMANCE_REPORT":
            return httpx.Response(200, content=_campaign_tsv(clicks="5", cost="22.34"))
        return httpx.Response(200, content=_query_tsv(query_rows))

    client, calls = _install(handler)
    try:
        response = client.get(
            "/yandex/reports/search-queries",
            params={"campaign_id": "710691939", "date_from": "2026-08-11", "date_to": "2026-08-11"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reconciliation"] == {
        "search_query_clicks": 5,
        "campaign_clicks": 5,
        "clicks_match": True,
        "search_query_cost": 22.34,
        "campaign_cost": 22.34,
        "cost_delta": 0.0,
        "cost_tolerance": 0.01,
        "cost_within_tolerance": True,
        "status": "matched",
    }
    report_calls = [call for call in calls if call["path"].endswith("/reports")]
    expected_filter = [{"Field": "CampaignId", "Operator": "IN", "Values": ["710691939"]}]
    assert [call["json"]["params"]["SelectionCriteria"]["Filter"] for call in report_calls] == [
        expected_filter,
        expected_filter,
    ]


def test_runtime_fix_reconciliation_accepts_row_rounding_delta_with_row_based_tolerance() -> None:
    query_rows = [
        [f"query-{index}", "710691939", "1001", "1", "11" if index == 0 else "0", "0.00", "57.15"]
        for index in range(50)
    ]
    query_rows.append(["query-50", "710691939", "1001", "1", "0", "0.00", "56.89"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json={"result": {"Campaigns": []}})
        report_type = json.loads(request.content.decode())["params"]["ReportType"]
        if report_type == "CAMPAIGN_PERFORMANCE_REPORT":
            return httpx.Response(200, content=_campaign_tsv(clicks="11", cost="2914.37"))
        return httpx.Response(200, content=_query_tsv(query_rows))

    client, _ = _install(handler)
    try:
        response = client.get("/yandex/reports/search-queries")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["search_query_clicks"] == 11
    assert reconciliation["campaign_clicks"] == 11
    assert reconciliation["clicks_match"] is True
    assert reconciliation["search_query_cost"] == 2914.39
    assert reconciliation["campaign_cost"] == 2914.37
    assert reconciliation["cost_delta"] == 0.02
    assert reconciliation["cost_tolerance"] == 0.26
    assert reconciliation["cost_within_tolerance"] is True
    assert reconciliation["status"] == "matched"


def test_runtime_fix_reconciliation_exposes_a_material_mismatch_without_rejecting_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json={"result": {"Campaigns": []}})
        report_type = json.loads(request.content.decode())["params"]["ReportType"]
        if report_type == "CAMPAIGN_PERFORMANCE_REPORT":
            return httpx.Response(200, content=_campaign_tsv(clicks="5", cost="10.05"))
        return httpx.Response(
            200,
            content=_query_tsv([["mismatch", "710691939", "1001", "2", "4", "100.00", "10.00"]]),
        )

    client, _ = _install(handler)
    try:
        response = client.get("/yandex/reports/search-queries")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["query"] for item in body["items"]] == ["mismatch"]
    assert body["reconciliation"] == {
        "search_query_clicks": 4,
        "campaign_clicks": 5,
        "clicks_match": False,
        "search_query_cost": 10.0,
        "campaign_cost": 10.05,
        "cost_delta": -0.05,
        "cost_tolerance": 0.01,
        "cost_within_tolerance": False,
        "status": "mismatch",
    }


def test_runtime_fix_reconciliation_failure_is_partial_and_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json={"result": {"Campaigns": []}})
        report_type = json.loads(request.content.decode())["params"]["ReportType"]
        if report_type == "CAMPAIGN_PERFORMANCE_REPORT":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "error_code": 4000,
                        "error_string": "Campaign report unavailable",
                        "error_detail": "Safe campaign detail",
                        "raw_body": "MUST-NOT-LEAK",
                    },
                    "token": "TEST-SECRET",
                },
            )
        return httpx.Response(
            200,
            content=_query_tsv([["still-visible", "710691939", "1001", "2", "1", "50.00", "10.00"]]),
        )

    client, _ = _install(handler)
    try:
        response = client.get("/yandex/reports/search-queries")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["query"] for item in body["items"]] == ["still-visible"]
    assert body["reconciliation"] is None
    assert body["partial_failure"] is True
    assert body["warnings"] == [
        {
            "code": "search_query_reconciliation_failure",
            "provider_error": {
                "provider": "yandex_direct",
                "service": "reports",
                "method": "POST",
                "report_type": "CAMPAIGN_PERFORMANCE_REPORT",
                "http_status": 400,
                "error_code": 4000,
                "error_string": "Campaign report unavailable",
                "error_detail": "Safe campaign detail",
            },
        }
    ]
    assert "TEST-SECRET" not in response.text
    assert "MUST-NOT-LEAK" not in response.text
