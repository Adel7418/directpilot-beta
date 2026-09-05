"""Characterize the canonical typed KeywordBids read route on current master."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.yandex_direct import YandexDirectClient


CAMPAIGN_ID = "123"


def _strategy_response() -> dict:
    return {
        "result": {
            "Campaigns": [
                {
                    "Id": int(CAMPAIGN_ID),
                    "TextCampaign": {
                        "BiddingStrategy": {
                            "Search": {"BiddingStrategyType": "HIGHEST_POSITION"}
                        }
                    },
                }
            ]
        }
    }


def test_keyword_bids_get_maps_money_pagination_and_autotargeting(
    client: TestClient,
    yandex_settings: Callable[..., Settings],
    override_dependencies: Callable[..., None],
    direct_client_factory: Callable[
        [Settings, Callable[[httpx.Request], httpx.Response]], YandexDirectClient
    ],
) -> None:
    calls: list[str] = []
    keyword_bids_request: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal keyword_bids_request
        body = json.loads(request.content)
        if request.url.path.endswith("/campaigns"):
            calls.append("campaigns.get")
            return httpx.Response(200, json=_strategy_response(), request=request)
        if request.url.path.endswith("/keywordbids"):
            calls.append("keywordbids.get")
            keyword_bids_request = body
            return httpx.Response(
                200,
                json={
                    "result": {
                        "KeywordBids": [
                            {
                                "KeywordId": 10,
                                "CampaignId": 123,
                                "AdGroupId": 20,
                                "ServingStatus": "ELIGIBLE",
                                "Search": {
                                    "Bid": 12_500_000,
                                    "AuctionBids": {
                                        "AuctionBidItems": [
                                            {
                                                "TrafficVolume": 100,
                                                "Bid": 83_860_000,
                                                "Price": 13_075_000,
                                            },
                                            {
                                                "TrafficVolume": 50,
                                                "Bid": 40_000_000,
                                                "Price": 5_000_000,
                                            },
                                        ]
                                    },
                                },
                            },
                            {
                                "KeywordId": 11,
                                "CampaignId": 123,
                                "AdGroupId": 21,
                                "Search": {"Bid": 5_000_000},
                            },
                        ],
                        "LimitedBy": 5,
                    }
                },
                request=request,
            )
        if request.url.path.endswith("/keywords"):
            calls.append("keywords.get")
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {
                                "Id": 10,
                                "CampaignId": 123,
                                "AdGroupId": 20,
                                "Keyword": "characterization phrase",
                            },
                            {
                                "Id": 11,
                                "CampaignId": 123,
                                "AdGroupId": 21,
                                "Keyword": "---autotargeting",
                            },
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"unexpected provider path: {request.url.path}")

    settings = yandex_settings("live_readonly", oauth_token="x")
    override_dependencies(settings, direct_client_factory(settings, handler))

    response = client.get(
        f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids",
        params=[
            ("keyword_ids", "10"),
            ("keyword_ids", "11"),
            ("limit", "200"),
            ("offset", "4"),
        ],
    )

    assert response.status_code == 200
    data = response.json()
    items_by_id = {item["keyword_id"]: item for item in data["items"]}
    normal = items_by_id[10]
    autotargeting = items_by_id[11]

    assert calls == ["campaigns.get", "keywordbids.get", "keywords.get"]
    assert keyword_bids_request is not None
    assert keyword_bids_request["method"] == "get"
    assert keyword_bids_request["params"]["SelectionCriteria"] == {
        "CampaignIds": [123],
        "KeywordIds": [10, 11],
    }
    assert keyword_bids_request["params"]["Page"] == {"Limit": 200, "Offset": 4}
    assert data["source"] == "yandex"
    assert data["read_only"] is True
    assert data["limited_by"] == 5
    assert data["next_offset"] == 6
    assert normal["row_kind"] == "keyword"
    assert normal["keyword"] == "characterization phrase"
    assert normal["search_bid_rub"] == 12.5
    assert [bid["traffic_volume"] for bid in normal["auction_bids"]] == [100, 50]
    assert normal["auction_bids"][0] == {
        "traffic_volume": 100,
        "bid_micros": 83_860_000,
        "bid_rub": 83.86,
        "price_micros": 13_075_000,
        "price_rub": 13.075,
    }
    assert autotargeting["row_kind"] == "autotargeting"
    assert autotargeting["keyword"] is None
    assert autotargeting["auction_bids"] == []


def test_keyword_bids_get_sends_all_requested_ids_in_one_v5_read(
    client: TestClient,
    yandex_settings: Callable[..., Settings],
    override_dependencies: Callable[..., None],
    direct_client_factory: Callable[
        [Settings, Callable[[httpx.Request], httpx.Response]], YandexDirectClient
    ],
) -> None:
    keyword_ids = list(range(1, 202))
    keyword_bid_batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if request.url.path.endswith("/keywordbids"):
            batch_ids = body["params"]["SelectionCriteria"]["KeywordIds"]
            keyword_bid_batch_sizes.append(len(batch_ids))
            return httpx.Response(
                200,
                json={
                    "result": {
                        "KeywordBids": [
                            {
                                "KeywordId": keyword_id,
                                "CampaignId": 123,
                                "AdGroupId": keyword_id + 1000,
                                "Search": {"Bid": 1_000_000},
                            }
                            for keyword_id in batch_ids
                        ]
                    }
                },
                request=request,
            )
        if request.url.path.endswith("/keywords"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {
                                "Id": keyword_id,
                                "CampaignId": 123,
                                "AdGroupId": keyword_id + 1000,
                                "Keyword": f"phrase {keyword_id}",
                            }
                            for keyword_id in keyword_ids
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"unexpected provider path: {request.url.path}")

    settings = yandex_settings("live_readonly", oauth_token="x")
    override_dependencies(settings, direct_client_factory(settings, handler))

    response = client.get(
        f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids",
        params=[("keyword_ids", str(keyword_id)) for keyword_id in keyword_ids],
    )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 201
    assert keyword_bid_batch_sizes == [201]
