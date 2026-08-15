from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.yandex_direct import YandexDirectClient


COUNT_FIELDS = {
    "Impressions",
    "Clicks",
    "ImpressionReach",
    "VideoViews",
    "VideoFirstQuartile",
    "VideoMidpoint",
    "VideoThirdQuartile",
    "VideoComplete",
}
FLOAT_FIELDS = {
    "Cost",
    "Ctr",
    "AvgCpc",
    "AvgEffectiveBid",
    "AvgImpressionPosition",
    "AvgClickPosition",
    "AvgTrafficVolume",
    "WeightedImpressions",
    "WeightedCtr",
    "Sessions",
    "BounceRate",
    "AvgPageviews",
    "Conversions",
    "ConversionRate",
    "CostPerConversion",
    "Revenue",
    "Profit",
    "GoalsRoi",
    "PurchaseRevenue",
    "PurchaseProfit",
    "PurchaseGoalsRoi",
    "AvgImpressionFrequency",
    "AvgCpm",
    "CPV",
    "AvgVideoCompleteCost",
    "VideoViewsRate",
    "VideoFirstQuartileRate",
    "VideoMidpointRate",
    "VideoThirdQuartileRate",
    "VideoCompleteRate",
}
TEXT_VALUES = {
    "Date": "2026-08-01",
    "CampaignId": "123",
    "CampaignName": "Campaign",
    "CampaignType": "TEXT_CAMPAIGN",
    "AdGroupId": "456",
    "AdGroupName": "Ad group",
    "AdId": "789",
    "AdFormat": "TEXT",
    "Criterion": "criterion",
    "CriterionId": "987",
    "CriterionType": "KEYWORD",
    "Query": "search query",
    "MatchedKeyword": "matched keyword",
    "MatchType": "KEYWORD",
    "AdNetworkType": "SEARCH",
    "Placement": "example.test",
}


def settings_for(mode: str = "live_readonly", token: str | None = "unit-test-token") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=token)


def make_direct_client(settings: Settings, handler: Callable[[httpx.Request], httpx.Response]) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


@contextmanager
def report_overrides(settings: Settings, direct_client: YandexDirectClient | None) -> Iterator[TestClient]:
    from app import main as main_mod

    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: direct_client
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)


def field_value(field: str, overrides: dict[str, str] | None = None) -> str:
    if overrides and field in overrides:
        return overrides[field]
    if field in COUNT_FIELDS:
        return "12"
    if field in FLOAT_FIELDS:
        return "1.25"
    return TEXT_VALUES.get(field, "value")


def report_tsv(
    fields: list[str],
    *,
    rows: list[dict[str, str]] | None = None,
    header_fields: list[str] | None = None,
) -> str:
    header_fields = header_fields or list(fields)
    rows = [{}] if rows is None else rows
    lines = ["\t".join(header_fields)]
    for overrides in rows:
        lines.append("\t".join(field_value(field, overrides) for field in header_fields))
    return "\n".join(lines) + "\n"
