from __future__ import annotations

import json
from typing import Any

import httpx

from tests.report_test_support import (
    make_direct_client,
    report_overrides,
    report_tsv,
    settings_for,
)


CAMPAIGN_CORE = [
    "Date",
    "CampaignId",
    "CampaignName",
    "CampaignType",
    "Impressions",
    "Clicks",
    "Cost",
    "Ctr",
    "AvgCpc",
]
CAMPAIGN_POSITIONS = [
    *CAMPAIGN_CORE,
    "AvgEffectiveBid",
    "AvgImpressionPosition",
    "AvgClickPosition",
    "AvgTrafficVolume",
    "WeightedImpressions",
    "WeightedCtr",
]


def _call_campaign(tsv: str, *, view: str = "core"):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, content=tsv)

    settings = settings_for()
    direct_client = make_direct_client(settings, handler)
    with report_overrides(settings, direct_client) as api:
        response = api.get(
            "/yandex/reports/campaign-performance",
            params={"date_from": "2026-08-01", "date_to": "2026-08-01", "view": view},
        )
    return response, captured


def test_average_position_metrics_parse_from_header_reordered_tsv():
    reversed_header = list(reversed(CAMPAIGN_POSITIONS))
    tsv = report_tsv(
        CAMPAIGN_POSITIONS,
        header_fields=reversed_header,
        rows=[
            {
                "AvgImpressionPosition": "3.50",
                "AvgClickPosition": "1.25",
                "AvgTrafficVolume": "74.00",
            }
        ],
    )

    response, captured = _call_campaign(tsv, view="positions")

    assert response.status_code == 200, response.text
    assert captured["payload"]["params"]["FieldNames"] == CAMPAIGN_POSITIONS
    item = response.json()["items"][0]
    assert item["avg_impression_position"] == 3.5
    assert item["avg_click_position"] == 1.25
    assert item["avg_traffic_volume"] == 74.0


def test_unavailable_direct_metrics_are_null_not_zero():
    tsv = report_tsv(
        CAMPAIGN_POSITIONS,
        rows=[
            {
                "Impressions": "--",
                "Cost": "--",
                "AvgImpressionPosition": "--",
                "AvgClickPosition": "--",
            }
        ],
    )

    response, _ = _call_campaign(tsv, view="positions")

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["impressions"] is None
    assert item["cost"] is None
    assert item["avg_impression_position"] is None
    assert item["avg_click_position"] is None


def test_header_only_completed_report_is_distinguishable_empty_result():
    tsv = report_tsv(CAMPAIGN_CORE, rows=[])

    response, _ = _call_campaign(tsv)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["parser_status"] == "empty"
    assert body["items"] == []
    assert body["row_count"] == 0
    assert body["rows_received"] == 0
    assert body["rows_parsed"] == 0
    assert body["rows_rejected"] == 0


def test_mixed_valid_and_invalid_rows_are_visible_partial_result():
    tsv = report_tsv(
        CAMPAIGN_CORE,
        rows=[{}, {"Impressions": "not-a-direct-count"}],
    )

    response, _ = _call_campaign(tsv)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["parser_status"] == "partial"
    assert body["row_count"] == 1
    assert body["rows_received"] == 2
    assert body["rows_parsed"] == 1
    assert body["rows_rejected"] == 1
    assert body["warnings"]


def test_nonempty_all_invalid_rows_fail_closed_as_report_parse_error():
    tsv = report_tsv(CAMPAIGN_CORE, rows=[{"Clicks": "not-a-direct-count"}])

    response, _ = _call_campaign(tsv)

    assert response.status_code == 502, response.text
    assert response.json()["detail"]["error_type"] == "report_parse_error"


def test_missing_required_header_fails_closed_as_report_parse_error():
    tsv = report_tsv([field for field in CAMPAIGN_CORE if field != "Cost"])

    response, _ = _call_campaign(tsv)

    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert detail["error_type"] == "report_parse_error"
    assert "Cost" in detail["missing_headers"]
