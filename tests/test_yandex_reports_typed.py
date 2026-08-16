from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from tests.report_test_support import (
    make_direct_client,
    report_overrides,
    report_tsv,
    settings_for,
)


CORE = ["Impressions", "Clicks", "Cost", "Ctr", "AvgCpc"]
REACH = [
    "Date",
    "CampaignId",
    "CampaignName",
    "CampaignType",
    "ImpressionReach",
    "AvgImpressionFrequency",
    "AvgCpm",
    "CPV",
    "AvgVideoCompleteCost",
    "VideoViews",
    "VideoViewsRate",
    "VideoFirstQuartile",
    "VideoFirstQuartileRate",
    "VideoMidpoint",
    "VideoMidpointRate",
    "VideoThirdQuartile",
    "VideoThirdQuartileRate",
    "VideoComplete",
    "VideoCompleteRate",
]

SURFACES = [
    (
        "account-performance",
        "ACCOUNT_PERFORMANCE_REPORT",
        ["Date", "CampaignType", *CORE],
        {},
    ),
    (
        "campaign-performance",
        "CAMPAIGN_PERFORMANCE_REPORT",
        ["Date", "CampaignId", "CampaignName", "CampaignType", *CORE],
        {},
    ),
    (
        "adgroup-performance",
        "ADGROUP_PERFORMANCE_REPORT",
        [
            "Date",
            "CampaignId",
            "CampaignName",
            "CampaignType",
            "AdGroupId",
            "AdGroupName",
            *CORE,
        ],
        {},
    ),
    (
        "ad-performance",
        "AD_PERFORMANCE_REPORT",
        [
            "Date",
            "CampaignId",
            "CampaignName",
            "CampaignType",
            "AdGroupId",
            "AdGroupName",
            "AdId",
            "AdFormat",
            *CORE,
        ],
        {},
    ),
    (
        "criteria-performance",
        "CRITERIA_PERFORMANCE_REPORT",
        [
            "Date",
            "CampaignId",
            "CampaignName",
            "CampaignType",
            "AdGroupId",
            "AdGroupName",
            "Criterion",
            "CriterionId",
            "CriterionType",
            "MatchType",
            *CORE,
        ],
        {},
    ),
    (
        "custom-performance",
        "CUSTOM_REPORT",
        ["Date", "CampaignId", "CampaignName", "CampaignType", *CORE],
        {},
    ),
    (
        "reach-frequency",
        "REACH_AND_FREQUENCY_PERFORMANCE_REPORT",
        REACH,
        {"campaign_id": "123"},
    ),
    (
        "search-queries",
        "SEARCH_QUERY_PERFORMANCE_REPORT",
        [
            "Date",
            "Query",
            "CampaignId",
            "CampaignName",
            "CampaignType",
            "AdGroupId",
            "AdGroupName",
            "Criterion",
            "CriterionId",
            "CriterionType",
            "MatchedKeyword",
            "MatchType",
            *CORE,
        ],
        {},
    ),
]


def test_reports_catalog_lists_only_the_eight_server_owned_presets():
    from app.main import app
    from fastapi.testclient import TestClient

    response = TestClient(app).get("/yandex/reports/catalog")

    assert response.status_code == 200, response.text
    catalog = response.json()
    assert {item["surface"] for item in catalog["items"]} == {case[0] for case in SURFACES}
    by_surface = {item["surface"]: item for item in catalog["items"]}
    for surface, report_type, fields, _ in SURFACES:
        assert by_surface[surface]["report_type"] == report_type
        assert by_surface[surface]["read_only"] is True
        assert by_surface[surface]["fields"]["core" if surface != "reach-frequency" else "reach"] == [
            _snake_case(field) for field in fields
        ]
    assert by_surface["search-queries"]["offline_only"] is True


def test_search_queries_catalog_does_not_advertise_placement_view():
    from app.main import app
    from fastapi.testclient import TestClient

    response = TestClient(app).get("/yandex/reports/catalog")

    assert response.status_code == 200, response.text
    search_queries = next(
        item for item in response.json()["items"] if item["surface"] == "search-queries"
    )
    assert search_queries["supported_views"] == ["core", "positions", "outcomes"]
    assert "placement" not in search_queries["fields"]


def _snake_case(name: str) -> str:
    import re

    if name == "CPV":
        return "cpv"
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@pytest.mark.parametrize("surface,report_type,fields,params", SURFACES)
def test_each_typed_surface_builds_its_documented_server_owned_fields(
    surface: str,
    report_type: str,
    fields: list[str],
    params: dict[str, str],
):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, content=report_tsv(captured["payload"]["params"]["FieldNames"]))

    settings = settings_for()
    direct_client = make_direct_client(settings, handler)
    with report_overrides(settings, direct_client) as api:
        response = api.get(
            f"/yandex/reports/{surface}",
            params={"date_from": "2026-08-01", "date_to": "2026-08-01", **params},
        )

    assert response.status_code == 200, response.text
    payload = captured["payload"]["params"]
    assert payload["ReportType"] == report_type
    assert payload["FieldNames"] == fields
    body = response.json()
    assert body["report_type"] == report_type
    assert body["surface"] == surface
    assert body["read_only"] is True
    assert body["source"] == "yandex"
    assert body["row_count"] == 1
    assert body["parser_status"] == "ok"


def test_completed_day_default_and_invalid_date_ranges_fail_before_transport():
    calls: list[httpx.Request] = []
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, content=report_tsv(captured["payload"]["params"]["FieldNames"]))

    settings = settings_for()
    direct_client = make_direct_client(settings, handler)
    with report_overrides(settings, direct_client) as api:
        default_response = api.get("/yandex/reports/campaign-performance")
        assert default_response.status_code == 200, default_response.text
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        selection = captured["payload"]["params"]["SelectionCriteria"]
        assert selection["DateFrom"] == yesterday
        assert selection["DateTo"] == yesterday

        calls.clear()
        one_date = api.get(
            "/yandex/reports/campaign-performance",
            params={"date_from": "2026-08-01"},
        )
        future = api.get(
            "/yandex/reports/campaign-performance",
            params={"date_from": "2999-01-01", "date_to": "2999-01-01"},
        )
        inverted = api.get(
            "/yandex/reports/campaign-performance",
            params={"date_from": "2026-08-02", "date_to": "2026-08-01"},
        )

    assert [response.status_code for response in (one_date, future, inverted)] == [422, 422, 422]
    assert calls == []


def test_incompatible_view_and_filter_are_rejected_before_transport():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content="should-not-be-used")

    settings = settings_for()
    direct_client = make_direct_client(settings, handler)
    with report_overrides(settings, direct_client) as api:
        incompatible_view = api.get(
            "/yandex/reports/reach-frequency",
            params={
                "campaign_id": "123",
                "date_from": "2026-08-01",
                "date_to": "2026-08-01",
                "view": "positions",
            },
        )
        incompatible_filter = api.get(
            "/yandex/reports/campaign-performance",
            params={
                "date_from": "2026-08-01",
                "date_to": "2026-08-01",
                "ad_group_id": "456",
            },
        )
        non_numeric_filter = api.get(
            "/yandex/reports/ad-performance",
            params={
                "date_from": "2026-08-01",
                "date_to": "2026-08-01",
                "ad_id": "not-an-id",
            },
        )

    assert [response.status_code for response in (incompatible_view, incompatible_filter, non_numeric_filter)] == [422, 422, 422]
    assert calls == []


def test_search_queries_placement_view_is_rejected_before_transport():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = json.loads(request.content.decode())
        return httpx.Response(200, content=report_tsv(payload["params"]["FieldNames"]))

    settings = settings_for()
    direct_client = make_direct_client(settings, handler)
    with report_overrides(settings, direct_client) as api:
        response = api.get(
            "/yandex/reports/search-queries",
            params={
                "date_from": "2026-08-01",
                "date_to": "2026-08-01",
                "view": "placement",
            },
        )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "view 'placement' is not supported for search-queries"
    assert calls == []


def test_typed_report_live_mode_without_client_stays_explicit_not_mock():
    settings = settings_for()
    with report_overrides(settings, None) as api:
        response = api.get("/yandex/reports/campaign-performance")

    assert response.status_code == 409, response.text
    assert "unit-test-token" not in response.text
