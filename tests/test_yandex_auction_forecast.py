"""Focused contract tests for the read-only Yandex auction forecast."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.yandex_direct import YandexDirectClient


CAMPAIGN_ID = "123"
client = TestClient(app)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        directpilot_mode="live_readonly",
        yandex_oauth_token="test-value",
    )


def _direct_client(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


def _reset_overrides() -> None:
    app.dependency_overrides.clear()


def test_auction_forecast_maps_sorted_levels_deduplicates_ids_and_documents_query_contract() -> None:
    settings = _settings()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            assert body == {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"CampaignIds": [123], "Ids": [10, 11]},
                    "FieldNames": [
                        "Id",
                        "AdGroupId",
                        "CampaignId",
                        "Keyword",
                        "Bid",
                        "ContextBid",
                        "StrategyPriority",
                        "State",
                        "Status",
                        "ServingStatus",
                    ],
                    "Page": {"Limit": 200, "Offset": 4},
                },
            }
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {
                                "Id": 10,
                                "CampaignId": 123,
                                "AdGroupId": 20,
                                "Keyword": "first phrase",
                                "Bid": 12_500_000,
                                "State": "ON",
                                "Status": "ACCEPTED",
                                "ServingStatus": "ELIGIBLE",
                            },
                            {
                                "Id": 11,
                                "CampaignId": 123,
                                "AdGroupId": 21,
                                "Keyword": "second phrase",
                                "Bid": 8_000_000,
                                "State": "ON",
                                "Status": "ACCEPTED",
                                "ServingStatus": "ELIGIBLE",
                            },
                        ],
                        "LimitedBy": 5,
                    }
                },
                request=request,
            )
        if "/campaigns" in str(request.url):
            calls.append("campaigns.get")
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Campaigns": [
                            {
                                "Id": 123,
                                "TextCampaign": {
                                    "BiddingStrategy": {
                                        "Search": {"BiddingStrategyType": "HIGHEST_POSITION"}
                                    }
                                },
                            }
                        ]
                    }
                },
                request=request,
            )
        if "/keywordbids" in str(request.url):
            calls.append("keywordbids.get")
            assert body == {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"CampaignIds": [123], "KeywordIds": [10, 11]},
                    "FieldNames": [
                        "KeywordId",
                        "AdGroupId",
                        "CampaignId",
                        "ServingStatus",
                        "StrategyPriority",
                    ],
                    "SearchFieldNames": ["Bid", "AutotargetingSearchBidIsAuto", "AuctionBids"],
                    "NetworkFieldNames": ["Bid"],
                    "Page": {"Limit": 200, "Offset": 0},
                },
            }
            return httpx.Response(
                200,
                json={
                    "result": {
                        "KeywordBids": [
                            {
                                "KeywordId": 10,
                                "CampaignId": 123,
                                "Search": {
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
                                    }
                                },
                            },
                            {
                                "KeywordId": 11,
                                "CampaignId": 123,
                                "Search": {
                                    "AuctionBids": {
                                        "AuctionBidItems": [
                                            {
                                                "TrafficVolume": 75,
                                                "Bid": 10_000_000,
                                                "Price": 7_000_000,
                                            }
                                        ]
                                    }
                                },
                            },
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.get(
            f"/yandex/campaigns/{CAMPAIGN_ID}/auction-forecast",
            params=[
                ("keyword_ids", "10"),
                ("keyword_ids", "10"),
                ("keyword_ids", "11"),
                ("page_token", "4"),
            ],
        )
        assert response.status_code == 200, response.text
        data = response.json()
    finally:
        _reset_overrides()

    assert calls == ["keywords.get", "campaigns.get", "keywordbids.get"]
    assert data["campaign_id"] == CAMPAIGN_ID
    assert data["source"] == "yandex"
    assert data["read_only"] is True
    assert data["next_page_token"] == "5"
    assert data["items"][0]["current_search_bid_micros"] == 12_500_000
    assert data["items"][0]["current_search_bid_rub"] == 12.5
    assert [bid["traffic_volume"] for bid in data["items"][0]["auction_bids"]] == [50, 100]
    assert data["items"][0]["auction_bids"][1] == {
        "traffic_volume": 100,
        "bid_micros": 83_860_000,
        "bid_rub": 83.86,
        "price_micros": 13_075_000,
        "price_rub": 13.075,
    }
    assert data["items"][0]["forecast_status"] == "AVAILABLE"
    assert data["items"][0]["forecast_reason"] is None

    operation = app.openapi()["paths"]["/yandex/campaigns/{campaign_id}/auction-forecast"]["get"]
    assert "requestBody" not in operation
    query_parameters = {
        parameter["name"]: parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "query"
    }
    assert set(query_parameters) == {"keyword_ids", "limit", "page_token"}
    assert query_parameters["limit"]["schema"]["default"] == 200
    assert "silently absent" in operation["description"]


def test_auction_forecast_omits_nonprogressing_limited_by_token() -> None:
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/keywords" in str(request.url):
            return httpx.Response(
                200,
                json={"result": {"Keywords": [], "LimitedBy": 4}},
                request=request,
            )
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_forecast_strategy(), request=request)
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.get(
            f"/yandex/campaigns/{CAMPAIGN_ID}/auction-forecast",
            params={"page_token": "4"},
        )
        assert response.status_code == 200, response.text
    finally:
        _reset_overrides()

    assert response.json()["next_page_token"] is None


def test_auction_forecast_marks_autotargeting_not_applicable_without_keywordbids_batch() -> None:
    settings = _settings()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            assert body["params"]["SelectionCriteria"] == {"CampaignIds": [123]}
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {
                                "Id": 10,
                                "CampaignId": 123,
                                "AdGroupId": 20,
                                "Keyword": "---autotargeting",
                                "Bid": 5_000_000,
                                "State": "ON",
                                "Status": "ACCEPTED",
                                "ServingStatus": "ELIGIBLE",
                            }
                        ]
                    }
                },
                request=request,
            )
        if "/campaigns" in str(request.url):
            calls.append("campaigns.get")
            assert body["method"] == "get"
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Campaigns": [
                            {
                                "Id": 123,
                                "TextCampaign": {
                                    "BiddingStrategy": {
                                        "Search": {"BiddingStrategyType": "SERVING_OFF"}
                                    }
                                },
                            }
                        ]
                    }
                },
                request=request,
            )
        if "/keywordbids" in str(request.url):
            raise AssertionError("autotargeting must not enter an AuctionBids batch")
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.get(f"/yandex/campaigns/{CAMPAIGN_ID}/auction-forecast")
        assert response.status_code == 200, response.text
        data = response.json()
    finally:
        _reset_overrides()

    assert calls == ["keywords.get"]
    assert data["items"] == [
        {
            "keyword_id": 10,
            "ad_group_id": 20,
            "phrase": None,
            "state": "ON",
            "status": "ACCEPTED",
            "serving_status": "ELIGIBLE",
            "current_search_bid_micros": 5_000_000,
            "current_search_bid_rub": 5.0,
            "auction_bids": [],
            "forecast_status": "NOT_APPLICABLE",
            "forecast_reason": "AUTOTARGETING",
        }
    ]


def _forecast_keyword(
    keyword_id: int,
    *,
    state: str = "ON",
    status: str = "ACCEPTED",
    serving_status: str = "ELIGIBLE",
) -> dict:
    return {
        "Id": keyword_id,
        "CampaignId": 123,
        "AdGroupId": keyword_id + 100,
        "Keyword": f"phrase {keyword_id}",
        "Bid": 1_000_000,
        "State": state,
        "Status": status,
        "ServingStatus": serving_status,
    }


def _forecast_strategy(search: str = "HIGHEST_POSITION") -> dict:
    return {
        "result": {
            "Campaigns": [
                {
                    "Id": 123,
                    "TextCampaign": {
                        "BiddingStrategy": {
                            "Search": {"BiddingStrategyType": search}
                        }
                    },
                }
            ]
        }
    }


def test_auction_forecast_normalizes_null_auction_reasons_without_fake_zero_values() -> None:
    settings = _settings()
    keywords = [
        _forecast_keyword(10, serving_status="RARELY_SERVED"),
        _forecast_keyword(11),
        _forecast_keyword(12, state="OFF"),
        _forecast_keyword(13, status="REJECTED"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "/keywords" in str(request.url):
            return httpx.Response(200, json={"result": {"Keywords": keywords}}, request=request)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_forecast_strategy(), request=request)
        if "/keywordbids" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "KeywordBids": [
                            {
                                "KeywordId": keyword["Id"],
                                "CampaignId": 123,
                                "Search": {"AuctionBids": None},
                            }
                            for keyword in keywords
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.get(f"/yandex/campaigns/{CAMPAIGN_ID}/auction-forecast")
        assert response.status_code == 200, response.text
        items = response.json()["items"]
    finally:
        _reset_overrides()

    assert [(item["forecast_status"], item["forecast_reason"], item["auction_bids"]) for item in items] == [
        ("UNAVAILABLE", "RARELY_SERVED", []),
        ("UNAVAILABLE", "NO_AUCTION_DATA", []),
        ("UNAVAILABLE", "KEYWORD_NOT_SERVING", []),
        ("UNAVAILABLE", "KEYWORD_NOT_SERVING", []),
    ]


def test_auction_forecast_rejects_fractional_traffic_volume_without_rounding() -> None:
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/keywords" in str(request.url):
            return httpx.Response(
                200,
                json={"result": {"Keywords": [_forecast_keyword(10)]}},
                request=request,
            )
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_forecast_strategy(), request=request)
        if "/keywordbids" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "KeywordBids": [
                            {
                                "KeywordId": 10,
                                "CampaignId": 123,
                                "Search": {
                                    "AuctionBids": {
                                        "AuctionBidItems": [
                                            {
                                                "TrafficVolume": 83.86,
                                                "Bid": 83_860_000,
                                                "Price": 13_075_000,
                                            }
                                        ]
                                    }
                                },
                            }
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.get(f"/yandex/campaigns/{CAMPAIGN_ID}/auction-forecast")
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
    finally:
        _reset_overrides()

    assert item["auction_bids"] == []
    assert item["forecast_status"] == "ERROR"
    assert item["forecast_reason"] == "INVALID_AUCTION_DATA"


def test_auction_forecast_continues_after_a_failed_200_item_batch_without_leaking_error() -> None:
    settings = _settings()
    keywords = [_forecast_keyword(keyword_id) for keyword_id in range(1, 202)]
    batch_calls: list[list[int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/keywords" in str(request.url):
            assert body["params"]["Page"] == {"Limit": 1000, "Offset": 0}
            return httpx.Response(200, json={"result": {"Keywords": keywords}}, request=request)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_forecast_strategy(), request=request)
        if "/keywordbids" in str(request.url):
            batch = body["params"]["SelectionCriteria"]["KeywordIds"]
            batch_calls.append(batch)
            assert len(batch) <= 200
            if batch[0] == 1:
                return httpx.Response(
                    200,
                    json={"error": {"error_string": "raw isolated batch failure"}},
                    request=request,
                )
            assert batch == [201]
            return httpx.Response(
                200,
                json={
                    "result": {
                        "KeywordBids": [
                            {
                                "KeywordId": 201,
                                "CampaignId": 123,
                                "Search": {
                                    "AuctionBids": {
                                        "AuctionBidItems": [
                                            {"TrafficVolume": 50, "Bid": 2_000_000, "Price": 1_000_000}
                                        ]
                                    }
                                },
                            }
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.get(
            f"/yandex/campaigns/{CAMPAIGN_ID}/auction-forecast",
            params=[("keyword_ids", str(keyword_id)) for keyword_id in range(1, 202)] + [("limit", "1000")],
        )
        assert response.status_code == 200, response.text
        data = response.json()
    finally:
        _reset_overrides()

    assert batch_calls == [list(range(1, 201)), [201]]
    assert all(item["forecast_reason"] == "UPSTREAM_BATCH_ERROR" for item in data["items"][:200])
    assert data["items"][200]["forecast_status"] == "AVAILABLE"
    assert "raw isolated batch failure" not in response.text


def test_auction_forecast_does_not_mask_programming_errors_as_provider_batch_errors() -> None:
    settings = _settings()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            return httpx.Response(
                200,
                json={"result": {"Keywords": [_forecast_keyword(10)]}},
                request=request,
            )
        if "/campaigns" in str(request.url):
            calls.append("campaigns.get")
            return httpx.Response(200, json=_forecast_strategy(), request=request)
        if "/keywordbids" in str(request.url):
            calls.append("keywordbids.get")
            raise RuntimeError("programming error")
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        with pytest.raises(RuntimeError, match="programming error"):
            client.get(f"/yandex/campaigns/{CAMPAIGN_ID}/auction-forecast")
    finally:
        _reset_overrides()

    assert calls == ["keywords.get", "campaigns.get", "keywordbids.get"]


def test_auction_forecast_marks_missing_duplicate_mismatched_and_malformed_rows_as_item_errors() -> None:
    settings = _settings()
    keywords = [_forecast_keyword(keyword_id) for keyword_id in range(1, 5)]

    def valid_row(keyword_id: int, *, campaign_id: int = 123) -> dict:
        return {
            "KeywordId": keyword_id,
            "CampaignId": campaign_id,
            "Search": {
                "AuctionBids": {
                    "AuctionBidItems": [{"TrafficVolume": 50, "Bid": 2_000_000, "Price": 1_000_000}]
                }
            },
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/keywords" in str(request.url):
            return httpx.Response(200, json={"result": {"Keywords": keywords}}, request=request)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_forecast_strategy(), request=request)
        if "/keywordbids" in str(request.url):
            malformed = valid_row(4)
            malformed["Search"]["AuctionBids"]["AuctionBidItems"][0]["Bid"] = "2_000_000"
            return httpx.Response(
                200,
                json={
                    "result": {
                        "KeywordBids": [
                            valid_row(2),
                            valid_row(2),
                            valid_row(3, campaign_id=999),
                            malformed,
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.get(f"/yandex/campaigns/{CAMPAIGN_ID}/auction-forecast")
        assert response.status_code == 200, response.text
        items = response.json()["items"]
    finally:
        _reset_overrides()

    assert [(item["forecast_status"], item["forecast_reason"]) for item in items] == [
        ("ERROR", "AUCTION_ROW_MISSING"),
        ("ERROR", "INVALID_AUCTION_DATA"),
        ("ERROR", "INVALID_AUCTION_DATA"),
        ("ERROR", "INVALID_AUCTION_DATA"),
    ]


def test_auction_forecast_rejects_invalid_page_token_before_provider_reads() -> None:
    response = client.get(f"/yandex/campaigns/{CAMPAIGN_ID}/auction-forecast?page_token=1.2")

    assert response.status_code == 422
    assert response.json()["detail"] == "page_token must be a non-negative decimal offset"


def test_auction_forecast_rejects_invalid_ids_before_provider_reads() -> None:
    settings = _settings()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            return httpx.Response(200, json={"result": {"Keywords": []}}, request=request)
        if "/campaigns" in str(request.url):
            calls.append("campaigns.get")
            return httpx.Response(200, json=_forecast_strategy(), request=request)
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        invalid_requests = [
            ("0", None, "campaign_id must be a positive integer"),
            ("not-an-integer", None, "campaign_id must be a positive integer"),
            (CAMPAIGN_ID, [("keyword_ids", "0")], "keyword_ids must contain positive integers"),
            (CAMPAIGN_ID, [("keyword_ids", "-1")], "keyword_ids must contain positive integers"),
        ]
        for campaign_id, params, detail in invalid_requests:
            response = client.get(
                f"/yandex/campaigns/{campaign_id}/auction-forecast",
                params=params,
            )
            assert response.status_code == 422, response.text
            assert response.json()["detail"] == detail
    finally:
        _reset_overrides()

    assert calls == []


def test_auction_forecast_rejects_more_than_1000_deduplicated_keyword_ids_before_provider_reads() -> None:
    settings = _settings()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"result": {"Keywords": []}}, request=request)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.get(
            f"/yandex/campaigns/{CAMPAIGN_ID}/auction-forecast",
            params=[("keyword_ids", str(keyword_id)) for keyword_id in range(1, 1002)],
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "keyword_ids must contain at most 1000 ids"
    finally:
        _reset_overrides()

    assert calls == []


def test_existing_bids_and_keyword_bids_read_contracts_remain_unchanged() -> None:
    settings = _settings()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/bids" in str(request.url):
            calls.append("bids.get")
            assert body == {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"CampaignIds": [123]},
                    "FieldNames": ["KeywordId", "AdGroupId", "CampaignId", "Bid", "ContextBid"],
                },
            }
            return httpx.Response(200, json={"result": {"Bids": []}}, request=request)
        if "/campaigns" in str(request.url):
            calls.append("campaigns.get")
            return httpx.Response(200, json=_forecast_strategy(), request=request)
        if "/keywordbids" in str(request.url):
            calls.append("keywordbids.get")
            assert body["params"]["SelectionCriteria"] == {"CampaignIds": [123]}
            assert body["params"]["SearchFieldNames"] == ["Bid", "AutotargetingSearchBidIsAuto", "AuctionBids"]
            assert body["params"]["NetworkFieldNames"] == ["Bid", "Coverage"]
            assert body["params"]["Page"] == {"Limit": 1000, "Offset": 0}
            return httpx.Response(200, json={"result": {"KeywordBids": []}}, request=request)
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            assert body == {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"CampaignIds": [123]},
                    "FieldNames": [
                        "Id",
                        "AdGroupId",
                        "CampaignId",
                        "Keyword",
                        "Bid",
                        "ContextBid",
                        "StrategyPriority",
                        "State",
                        "Status",
                        "ServingStatus",
                    ],
                },
            }
            return httpx.Response(200, json={"result": {"Keywords": []}}, request=request)
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        legacy = client.get(f"/yandex/campaigns/{CAMPAIGN_ID}/bids")
    finally:
        _reset_overrides()

    assert legacy.status_code == 200, legacy.text
    assert calls == ["bids.get"]


def test_auction_forecast_skips_keywordbids_when_search_serving_is_off() -> None:
    settings = _settings()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            return httpx.Response(
                200,
                json={"result": {"Keywords": [_forecast_keyword(10)]}},
                request=request,
            )
        if "/campaigns" in str(request.url):
            calls.append("campaigns.get")
            return httpx.Response(200, json=_forecast_strategy("SERVING_OFF"), request=request)
        if "/keywordbids" in str(request.url):
            raise AssertionError("Search SERVING_OFF must not request AuctionBids")
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.get(f"/yandex/campaigns/{CAMPAIGN_ID}/auction-forecast")
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
    finally:
        _reset_overrides()

    assert calls == ["keywords.get", "campaigns.get"]
    assert item["auction_bids"] == []
    assert item["forecast_status"] == "UNAVAILABLE"
    assert item["forecast_reason"] == "SEARCH_SERVING_OFF"
