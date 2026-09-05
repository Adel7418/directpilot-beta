"""Characterize campaign and report behavior with in-process provider fakes."""

from __future__ import annotations

from collections.abc import Callable

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.yandex_direct import YandexDirectClient


def test_campaign_draft_list_and_detail_remain_available_in_mock_mode(
    client: TestClient,
    yandex_settings: Callable[..., Settings],
    override_dependencies: Callable[..., None],
) -> None:
    override_dependencies(yandex_settings("mock"))

    created = client.post(
        "/campaign-drafts",
        json={
            "business_type": "remont",
            "region": "Kazan",
            "monthly_budget": 45000,
            "landing_url": "https://example.invalid/characterization",
        },
    )
    assert created.status_code == 200
    draft = created.json()

    listed = client.get("/campaign-drafts")
    detail = client.get(f"/campaign-drafts/{draft['id']}")

    assert listed.status_code == 200
    assert any(item["id"] == draft["id"] for item in listed.json()["items"])
    assert detail.status_code == 200
    assert detail.json()["id"] == draft["id"]
    assert detail.json()["status"] == "draft"


def test_campaign_provider_success_and_error_mapping_use_the_fake_client(
    client: TestClient,
    yandex_settings: Callable[..., Settings],
    override_dependencies: Callable[..., None],
) -> None:
    class CampaignProvider:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def campaigns_get(self) -> dict:
            self.calls.append("campaigns.get")
            if self.calls.count("campaigns.get") == 2:
                return {"ok": False, "error": {"error_code": 53}}
            return {
                "ok": True,
                "result": {
                    "Campaigns": [
                        {
                            "Id": 410,
                            "Name": "Characterization campaign",
                            "Status": "ACTIVE",
                            "State": "ON",
                            "Type": "TEXT_CAMPAIGN",
                        }
                    ]
                },
            }

        def adgroups_get(self, campaign_id: str) -> dict:
            self.calls.append(f"adgroups.get:{campaign_id}")
            return {
                "ok": True,
                "result": {
                    "AdGroups": [
                        {
                            "Id": 411,
                            "CampaignId": int(campaign_id),
                            "Name": "Characterization ad group",
                            "Status": "ACTIVE",
                            "Type": "TEXT_AD_GROUP",
                        }
                    ]
                },
            }

    provider = CampaignProvider()
    override_dependencies(yandex_settings("live_readonly"), provider)

    campaign_list = client.get("/yandex/campaigns")
    related_detail = client.get("/yandex/campaigns/410/ad-groups")
    provider_error = client.get("/yandex/campaigns")

    assert campaign_list.status_code == 200
    assert campaign_list.json()["source"] == "yandex"
    assert campaign_list.json()["read_only"] is True
    assert campaign_list.json()["items"][0]["id"] == "410"
    assert related_detail.status_code == 200
    assert related_detail.json()["items"][0]["campaign_id"] == "410"
    assert provider_error.status_code == 502
    assert provider_error.json()["detail"]["error_type"] == "YandexDirectError"
    assert provider.calls == ["campaigns.get", "adgroups.get:410", "campaigns.get"]


def test_search_query_report_polls_parses_and_normalizes_direct_ids(
    client: TestClient,
    yandex_settings: Callable[..., Settings],
    override_dependencies: Callable[..., None],
    direct_client_factory: Callable[
        [Settings, Callable[[httpx.Request], httpx.Response]], YandexDirectClient
    ],
) -> None:
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append({name.lower(): value for name, value in request.headers.items()})
        return httpx.Response(
            200,
            content=(
                "Query\tCampaignId\tCampaignName\tAdGroupId\tImpressions\tClicks\tCtr\tCost\n"
                "characterization query\t000123\tExample\t700\t10\t2\t20\t31.5\n"
                "malformed metric\t000123\tExample\t700\tnot-an-int\t2\t20\t31.5\n"
                "different campaign\t99\tOther\t701\t9\t1\t11.1\t20\n"
            ),
            request=request,
        )

    settings = yandex_settings("live_readonly", oauth_token="x")
    override_dependencies(settings, direct_client_factory(settings, handler))

    response = client.get(
        "/yandex/reports/search-queries",
        params={
            "date_from": "2026-01-01",
            "date_to": "2026-01-02",
            "campaign_id": "000123",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert body["period"] == "2026-01-01..2026-01-02"
    assert body["items"] == [
        {
            "query": "characterization query",
            "campaign_id": "123",
            "campaign_name": "Example",
            "ad_group_id": "700",
            "impressions": 10,
            "clicks": 2,
            "ctr": 20.0,
            "cost": 31.5,
        }
    ]
    assert len(requests) == 1
    assert all(headers["processingmode"] == "auto" for headers in requests)
    assert all(headers["skipreportheader"] == "true" for headers in requests)
    assert all(headers["skipreportsummary"] == "true" for headers in requests)


def test_search_query_report_returns_empty_items_for_async_response(
    client: TestClient,
    yandex_settings: Callable[..., Settings],
    override_dependencies: Callable[..., None],
    direct_client_factory: Callable[
        [Settings, Callable[[httpx.Request], httpx.Response]], YandexDirectClient
    ],
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(202, headers={"retryIn": "0"}, request=request)

    settings = yandex_settings("live_readonly", oauth_token="x")
    override_dependencies(settings, direct_client_factory(settings, handler))

    response = client.get(
        "/yandex/reports/search-queries",
        params={"date_from": "2026-01-01", "date_to": "2026-01-02"},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "yandex"
    assert response.json()["read_only"] is True
    assert response.json()["items"] == []
    assert attempts == 1
