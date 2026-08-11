"""Focused contract tests for KeywordBids.get and keywordbids.setAuto.

All upstream interactions use ``httpx.MockTransport``.  No test makes a real
Yandex request or changes advertising settings.
"""

from __future__ import annotations

import json
from collections import Counter

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.models import KeywordBidsSetAutoRequest
from app.yandex_direct import YandexDirectClient


client = TestClient(app)
CAMPAIGN_ID = "123"


def _settings(mode: str) -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token="test-value")


def _reset_overrides() -> None:
    app.dependency_overrides.clear()


def _direct_client(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


def _strategy_response(*, search: str = "HIGHEST_POSITION", network: str = "MAXIMUM_COVERAGE") -> dict:
    return {
        "result": {
            "Campaigns": [
                {
                    "Id": 123,
                    "Type": "TEXT_CAMPAIGN",
                    "TextCampaign": {
                        "BiddingStrategy": {
                            "Search": {"BiddingStrategyType": search},
                            "Network": {"BiddingStrategyType": network},
                        }
                    },
                }
            ]
        }
    }


def _keyword_bids_response(*, limited_by: int | None = None) -> dict:
    result: dict = {
        "KeywordBids": [
            {
                "KeywordId": 10,
                "AdGroupId": 20,
                "CampaignId": 123,
                "ServingStatus": "ELIGIBLE",
                "StrategyPriority": "NORMAL",
                "Search": {
                    "Bid": 12_500_000,
                    "AutotargetingSearchBidIsAuto": "NO",
                    "AuctionBids": {
                        "AuctionBidItems": [
                            {"TrafficVolume": 75, "Bid": 11_000_000, "Price": 9_000_000}
                        ]
                    },
                },
                "Network": {
                    "Bid": 10_000_000,
                    "Coverage": {"CoverageItems": [{"Probability": 50, "Bid": 8_000_000}]},
                },
            },
            {
                "KeywordId": 11,
                "AdGroupId": 20,
                "CampaignId": 123,
                "ServingStatus": "RARELY_SERVED",
                "Search": {"Bid": 7_000_000, "AuctionBids": None},
                "Network": {"Bid": 6_000_000, "Coverage": None},
            },
            {
                "KeywordId": 12,
                "AdGroupId": 21,
                "CampaignId": 123,
                "Search": {"Bid": None},
                "Network": {"Bid": None},
            },
        ]
    }
    if limited_by is not None:
        result["LimitedBy"] = limited_by
    return {"result": result}


def _keywords_response() -> dict:
    return {
        "result": {
            "Keywords": [
                {"Id": 10, "CampaignId": 123, "AdGroupId": 20, "Keyword": "regular phrase"},
                {"Id": 11, "CampaignId": 123, "AdGroupId": 20, "Keyword": "---autotargeting"},
            ]
        }
    }


def test_keyword_bids_get_openapi_uses_optional_query_params_without_request_body() -> None:
    operation = app.openapi()["paths"]["/yandex/campaigns/{campaign_id}/keyword-bids"]["get"]

    assert "requestBody" not in operation
    query_parameters = {
        parameter["name"]: parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "query"
    }
    assert set(query_parameters) == {
        "ad_group_ids",
        "keyword_ids",
        "serving_statuses",
        "limit",
        "offset",
    }
    assert all(parameter["required"] is False for parameter in query_parameters.values())
    assert query_parameters["ad_group_ids"]["schema"]["anyOf"][0] == {
        "items": {"type": "integer"},
        "type": "array",
    }
    assert query_parameters["keyword_ids"]["schema"]["anyOf"][0] == {
        "items": {"type": "integer"},
        "type": "array",
    }
    assert query_parameters["serving_statuses"]["schema"]["anyOf"][0] == {
        "items": {"type": "string"},
        "type": "array",
    }
    assert query_parameters["limit"]["schema"]["default"] == 1000
    assert query_parameters["offset"]["schema"]["default"] == 0


def test_keywordbids_get_wrapper_uses_controlled_lowercase_service_and_fields() -> None:
    settings = _settings("live_readonly")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/keywordbids")
        body = json.loads(request.content)
        assert body == {
            "method": "get",
            "params": {
                "SelectionCriteria": {
                    "CampaignIds": [123],
                    "AdGroupIds": [20],
                    "KeywordIds": [10],
                    "ServingStatuses": ["ELIGIBLE"],
                },
                "FieldNames": ["KeywordId", "AdGroupId", "CampaignId", "ServingStatus", "StrategyPriority"],
                "SearchFieldNames": ["Bid", "AutotargetingSearchBidIsAuto", "AuctionBids"],
                "NetworkFieldNames": ["Bid", "Coverage"],
                "Page": {"Limit": 25, "Offset": 5},
            },
        }
        return httpx.Response(200, json={"result": {"KeywordBids": []}}, request=request)

    direct = _direct_client(settings, handler)
    response = direct.keywordbids_get(
        123,
        ad_group_ids=[20],
        keyword_ids=[10],
        serving_statuses=["ELIGIBLE"],
        limit=25,
        offset=5,
    )
    assert response["ok"] is True


def test_set_auto_model_rejects_mixed_or_empty_scope_and_builds_exact_micros_payload() -> None:
    with pytest.raises(ValidationError):
        KeywordBidsSetAutoRequest.model_validate(
            {
                "scope": "ad_group",
                "ad_group_ids": [20],
                "keyword_ids": [10],
                "rule": {"type": "search_by_traffic_volume", "target_traffic_volume": 75, "bid_ceiling_rub": 12.5},
            }
        )
    with pytest.raises(ValidationError):
        KeywordBidsSetAutoRequest.model_validate(
            {
                "scope": "keyword",
                "keyword_ids": [],
                "rule": {"type": "search_by_traffic_volume", "target_traffic_volume": 75, "bid_ceiling_rub": 12.5},
            }
        )

    request = KeywordBidsSetAutoRequest.model_validate(
        {
            "scope": "ad_group",
            "ad_group_ids": [20, 21],
            "rule": {
                "type": "search_by_traffic_volume",
                "target_traffic_volume": 75,
                "increase_percent": 10,
                "bid_ceiling_rub": 12.5,
            },
        }
    )
    assert request.build_direct_payload(123) == {
        "method": "setAuto",
        "params": {
            "KeywordBids": [
                {
                    "AdGroupId": 20,
                    "BiddingRule": {
                        "SearchByTrafficVolume": {
                            "TargetTrafficVolume": 75,
                            "IncreasePercent": 10,
                            "BidCeiling": 12_500_000,
                        }
                    },
                },
                {
                    "AdGroupId": 21,
                    "BiddingRule": {
                        "SearchByTrafficVolume": {
                            "TargetTrafficVolume": 75,
                            "IncreasePercent": 10,
                            "BidCeiling": 12_500_000,
                        }
                    },
                },
            ]
        },
    }


def test_keyword_bids_get_route_maps_rows_and_limited_by_pagination() -> None:
    settings = _settings("live_readonly")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywordbids" in str(request.url):
            assert body["method"] == "get"
            return httpx.Response(200, json=_keyword_bids_response(limited_by=7), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(200, json=_keywords_response(), request=request)
        pytest.fail(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.get(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids",
            params={"limit": 2, "offset": 5, "ad_group_ids": [20]},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["read_only"] is True
        assert data["limited_by"] == 7
        assert data["next_offset"] == 8
        assert [item["row_kind"] for item in data["items"]] == ["keyword", "autotargeting", "unknown"]
        assert data["items"][0]["search_bid_rub"] == 12.5
        assert data["items"][0]["auction_bids"][0]["price_rub"] == 9.0
        assert data["items"][1]["auction_bids"] == []
    finally:
        _reset_overrides()


def test_set_auto_preview_is_non_mutating_and_blocks_incompatible_strategy() -> None:
    settings = _settings("live_readonly")
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            calls["campaigns.get"] += 1
            return httpx.Response(200, json=_strategy_response(network="SERVING_OFF"), request=request)
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "get":
                return httpx.Response(200, json=_keyword_bids_response(), request=request)
            pytest.fail("setAuto must not be called for a blocked preview")
        if "/keywords" in str(request.url):
            calls["keywords.get"] += 1
            return httpx.Response(200, json=_keywords_response(), request=request)
        pytest.fail(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    body = {
        "scope": "campaign",
        "rule": {"type": "network_by_coverage", "target_coverage": 50, "bid_ceiling_rub": 10},
    }
    try:
        response = client.post(f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto", json=body)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["dry_run"] is True
        assert data["applied"] is False
        assert data["blocked"] is True
        assert data["payload_preview"]["method"] == "setAuto"
        assert calls["setAuto"] == 0
    finally:
        _reset_overrides()


def test_set_auto_keyword_preview_allows_limited_by_when_requested_id_is_returned() -> None:
    settings = _settings("live_readonly")
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "get":
                return httpx.Response(
                    200,
                    json=_keyword_bids_response(limited_by=1),
                    request=request,
                )
            pytest.fail("setAuto must not be called for a dry-run preview")
        if "/campaigns" in str(request.url):
            assert body["method"] == "get"
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(200, json=_keywords_response(), request=request)
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto",
            json={
                "scope": "keyword",
                "keyword_ids": [10],
                "rule": {
                    "type": "search_by_traffic_volume",
                    "target_traffic_volume": 80,
                    "increase_percent": 0,
                    "bid_ceiling_rub": 2_000,
                },
                "dry_run": True,
                "approved": False,
                "idempotency_key": "limitedby-keyword-preview-1",
                "reason": "Preview a single returned keyword.",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["blocked"] is False
        assert data["applied"] is False
        assert [item["keyword_id"] for item in data["affected_items"]] == [10, 11, 12]
        assert calls["setAuto"] == 0
    finally:
        _reset_overrides()


def test_set_auto_keyword_preview_allows_limited_by_when_all_requested_ids_are_returned() -> None:
    settings = _settings("live_readonly")
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "get":
                assert body["params"]["SelectionCriteria"]["KeywordIds"] == [10, 11]
                return httpx.Response(
                    200,
                    json=_keyword_bids_response(limited_by=2),
                    request=request,
                )
            pytest.fail("setAuto must not be called for a dry-run preview")
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {"Id": 10, "CampaignId": 123, "Keyword": "regular phrase"},
                            {"Id": 11, "CampaignId": 123, "Keyword": "another regular phrase"},
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto",
            json={
                "scope": "keyword",
                "keyword_ids": [10, 11],
                "rule": {
                    "type": "search_by_traffic_volume",
                    "target_traffic_volume": 80,
                    "bid_ceiling_rub": 2_000,
                },
                "dry_run": True,
                "approved": False,
                "idempotency_key": "limitedby-keyword-preview-many",
                "reason": "Preview all returned keywords.",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["blocked"] is False
        assert data["applied"] is False
        assert calls["setAuto"] == 0
    finally:
        _reset_overrides()


def test_set_auto_preview_blocks_missing_keyword_id_without_limited_by_before_provider_write() -> None:
    settings = _settings("live_write")
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "get":
                assert body["params"]["SelectionCriteria"]["KeywordIds"] == [10, 11]
                response = _keyword_bids_response()
                response["result"]["KeywordBids"] = response["result"]["KeywordBids"][:1]
                return httpx.Response(200, json=response, request=request)
            pytest.fail("setAuto must not be called when a requested keyword is missing")
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {"Id": 10, "CampaignId": 123, "Keyword": "regular phrase"},
                            {"Id": 11, "CampaignId": 123, "Keyword": "another regular phrase"},
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto",
            json={
                "scope": "keyword",
                "keyword_ids": [10, 11],
                "rule": {
                    "type": "search_by_traffic_volume",
                    "target_traffic_volume": 80,
                    "bid_ceiling_rub": 2_000,
                },
                "dry_run": True,
                "approved": False,
                "idempotency_key": "keyword-missing-no-limited-by-preview",
                "reason": "Block an incomplete unbounded keyword result.",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["dry_run"] is True
        assert data["blocked"] is True
        assert data["applied"] is False
        assert data["blockers"] == ["setAuto readback does not contain requested keyword IDs: [11]"]
        assert calls["setAuto"] == 0
    finally:
        _reset_overrides()


def test_set_auto_blocks_missing_keyword_id_from_limited_result_before_provider_write() -> None:
    settings = _settings("live_write")
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "get":
                response = _keyword_bids_response(limited_by=2)
                response["result"]["KeywordBids"] = response["result"]["KeywordBids"][:1]
                return httpx.Response(200, json=response, request=request)
            pytest.fail("setAuto must not be called when a requested keyword is missing")
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {"Id": 10, "CampaignId": 123, "Keyword": "regular phrase"},
                            {"Id": 11, "CampaignId": 123, "Keyword": "another regular phrase"},
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto",
            json={
                "scope": "keyword",
                "keyword_ids": [10, 11],
                "rule": {
                    "type": "search_by_traffic_volume",
                    "target_traffic_volume": 80,
                    "bid_ceiling_rub": 2_000,
                },
                "dry_run": False,
                "approved": True,
                "idempotency_key": "limitedby-keyword-missing-prewrite",
                "reason": "Block an incomplete keyword result.",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["blocked"] is True
        assert data["blockers"] == ["setAuto readback does not contain requested keyword IDs: [11]"]
        assert calls["setAuto"] == 0
    finally:
        _reset_overrides()


def test_set_auto_blocks_limited_keyword_result_with_unverified_ownership_before_provider_write() -> None:
    settings = _settings("live_write")
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "get":
                response = _keyword_bids_response(limited_by=1)
                response["result"]["KeywordBids"] = response["result"]["KeywordBids"][:1]
                response["result"]["KeywordBids"][0]["CampaignId"] = 999
                return httpx.Response(200, json=response, request=request)
            pytest.fail("setAuto must not be called when ownership is unverified")
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(200, json=_keywords_response(), request=request)
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto",
            json={
                "scope": "keyword",
                "keyword_ids": [10],
                "rule": {
                    "type": "search_by_traffic_volume",
                    "target_traffic_volume": 80,
                    "bid_ceiling_rub": 2_000,
                },
                "dry_run": False,
                "approved": True,
                "idempotency_key": "limitedby-keyword-ownership-prewrite",
                "reason": "Block unverified keyword ownership.",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["blocked"] is True
        assert data["blockers"] == ["setAuto ownership could not be verified for keyword IDs: [10]"]
        assert calls["setAuto"] == 0
    finally:
        _reset_overrides()


def test_set_auto_apply_accepts_complete_limited_keyword_readback() -> None:
    settings = _settings("live_write")
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(200, json=_keywords_response(), request=request)
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "setAuto":
                return httpx.Response(
                    200,
                    json={"result": {"SetAutoResults": [{"KeywordId": 10}]}},
                    request=request,
                )
            return httpx.Response(
                200,
                json=_keyword_bids_response(limited_by=1),
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto",
            json={
                "scope": "keyword",
                "keyword_ids": [10],
                "rule": {
                    "type": "search_by_traffic_volume",
                    "target_traffic_volume": 75,
                    "bid_ceiling_rub": 12.5,
                },
                "dry_run": False,
                "approved": True,
                "idempotency_key": "limitedby-keyword-complete-readback",
                "reason": "Apply only after complete keyword readback.",
            },
        )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["applied"] is True
        assert data["partial_failure"] is False
        assert data["readback_failed"] is False
        assert data["readback"]["items"][0]["keyword_id"] == 10
        assert calls["setAuto"] == 1
        assert calls["get"] == 2
    finally:
        _reset_overrides()


def test_set_auto_reports_missing_keyword_from_post_write_readback() -> None:
    settings = _settings("live_write")
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {"Id": 10, "CampaignId": 123, "Keyword": "regular phrase"},
                            {"Id": 11, "CampaignId": 123, "Keyword": "another regular phrase"},
                        ]
                    }
                },
                request=request,
            )
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "setAuto":
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "SetAutoResults": [{"KeywordId": 10}, {"KeywordId": 11}]
                        }
                    },
                    request=request,
                )
            response = _keyword_bids_response()
            if calls["get"] == 2:
                response["result"]["KeywordBids"] = response["result"]["KeywordBids"][:1]
            return httpx.Response(200, json=response, request=request)
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto",
            json={
                "scope": "keyword",
                "keyword_ids": [10, 11],
                "rule": {
                    "type": "search_by_traffic_volume",
                    "target_traffic_volume": 75,
                    "bid_ceiling_rub": 12.5,
                },
                "dry_run": False,
                "approved": True,
                "idempotency_key": "keyword-missing-post-write-readback",
                "reason": "Verify every requested keyword after provider write.",
            },
        )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["applied"] is False
        assert data["partial_failure"] is True
        assert data["readback_failed"] is True
        assert data["readback"] is None
        assert data["verification_error"] == "setAuto readback does not contain requested keyword IDs: [11]"
        assert calls["setAuto"] == 1
    finally:
        _reset_overrides()


def test_set_auto_apply_is_gated_idempotent_and_reads_back_keywordbids() -> None:
    settings = _settings("live_write")
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            calls["campaigns.get"] += 1
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywords" in str(request.url):
            calls["keywords.get"] += 1
            return httpx.Response(200, json=_keywords_response(), request=request)
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "setAuto":
                assert body["params"]["KeywordBids"] == [
                    {
                        "KeywordId": 10,
                        "BiddingRule": {
                            "SearchByTrafficVolume": {
                                "TargetTrafficVolume": 75,
                                "IncreasePercent": 0,
                                "BidCeiling": 12_500_000,
                            }
                        },
                    }
                ]
                return httpx.Response(
                    200,
                    json={"result": {"SetAutoResults": [{"KeywordId": 10}]}},
                    request=request,
                )
            return httpx.Response(200, json=_keyword_bids_response(), request=request)
        pytest.fail(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    body = {
        "scope": "keyword",
        "keyword_ids": [10],
        "rule": {"type": "search_by_traffic_volume", "target_traffic_volume": 75, "bid_ceiling_rub": 12.5},
        "dry_run": False,
        "approved": True,
        "idempotency_key": "set-auto-replay-001",
    }
    try:
        first = client.post(f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto", json=body)
        second = client.post(f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto", json=body)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        data = first.json()
        assert data["applied"] is True
        assert data["readback"]["items"][0]["keyword_id"] == 10
        assert calls["setAuto"] == 1
        assert calls["get"] >= 2

        changed = {**body, "rule": {"type": "search_by_traffic_volume", "target_traffic_volume": 80, "bid_ceiling_rub": 12.5}}
        conflict = client.post(f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto", json=changed)
        assert conflict.status_code == 409
        assert calls["setAuto"] == 1
    finally:
        _reset_overrides()


def test_set_auto_readback_failure_is_not_reported_as_applied() -> None:
    settings = _settings("live_write")
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(200, json=_keywords_response(), request=request)
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "setAuto":
                return httpx.Response(
                    200,
                    json={"result": {"SetAutoResults": [{"KeywordId": 10}]}},
                    request=request,
                )
            if calls["get"] == 2:
                raise httpx.ReadError("provider readback failed", request=request)
            return httpx.Response(200, json=_keyword_bids_response(), request=request)
        pytest.fail(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    body = {
        "scope": "keyword",
        "keyword_ids": [10],
        "rule": {"type": "search_by_traffic_volume", "target_traffic_volume": 75, "bid_ceiling_rub": 12.5},
        "dry_run": False,
        "approved": True,
        "idempotency_key": "set-auto-readback-failure-001",
    }
    try:
        response = client.post(f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto", json=body)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["applied"] is False
        assert data["partial_failure"] is True
        assert data["readback_failed"] is True
        assert data["verification_error"] == "keywordbids.get readback failed after provider write"
        assert "provider readback failed" not in data["verification_error"]
        assert calls["setAuto"] == 1
    finally:
        _reset_overrides()


def test_set_auto_keyword_preview_uses_one_page_for_more_than_1000_keyword_ids() -> None:
    settings = _settings("live_readonly")
    keyword_ids = list(range(10_000, 11_001))
    calls: Counter[str] = Counter()
    pages: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {"Id": keyword_id, "CampaignId": 123, "Keyword": "regular phrase"}
                            for keyword_id in keyword_ids
                        ]
                    }
                },
                request=request,
            )
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "get":
                assert body["params"]["SelectionCriteria"]["KeywordIds"] == keyword_ids
                pages.append(body["params"]["Page"])
                return httpx.Response(200, json={"result": {"KeywordBids": []}}, request=request)
            raise AssertionError("setAuto must not be called for a dry-run preview")
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    body = {
        "scope": "keyword",
        "keyword_ids": keyword_ids,
        "rule": {"type": "search_by_traffic_volume", "target_traffic_volume": 75, "bid_ceiling_rub": 12.5},
    }
    try:
        response = client.post(f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto", json=body)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["blocked"] is True
        assert data["applied"] is False
        assert pages == [{"Limit": len(keyword_ids), "Offset": 0}]
        assert calls["setAuto"] == 0
    finally:
        _reset_overrides()


def test_set_auto_apply_blocks_truncated_campaign_preview_before_provider_write() -> None:
    settings = _settings("live_write")
    calls: Counter[str] = Counter()
    pages: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(200, json=_keywords_response(), request=request)
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "get":
                pages.append(body["params"]["Page"])
                return httpx.Response(
                    200,
                    json=_keyword_bids_response(limited_by=10_000),
                    request=request,
                )
            raise AssertionError("setAuto must not be called when the preview is truncated")
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    body = {
        "scope": "campaign",
        "rule": {"type": "search_by_traffic_volume", "target_traffic_volume": 75, "bid_ceiling_rub": 12.5},
        "dry_run": False,
        "approved": True,
        "idempotency_key": "set-auto-truncated-preview-001",
    }
    try:
        response = client.post(f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto", json=body)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["applied"] is False
        assert data["blocked"] is True
        assert "safely verifiable" in data["blockers"][0]
        assert pages == [{"Limit": 10_000, "Offset": 0}]
        assert calls["setAuto"] == 0
    finally:
        _reset_overrides()


def test_set_auto_reports_truncated_ad_group_readback_as_unverified_after_provider_write() -> None:
    settings = _settings("live_write")
    calls: Counter[str] = Counter()
    pages: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy_response(), request=request)
        if "/adgroups" in str(request.url):
            return httpx.Response(
                200,
                json={"result": {"AdGroups": [{"Id": 20, "CampaignId": 123}]}},
                request=request,
            )
        if "/keywords" in str(request.url):
            return httpx.Response(200, json=_keywords_response(), request=request)
        if "/keywordbids" in str(request.url):
            calls[body["method"]] += 1
            if body["method"] == "setAuto":
                return httpx.Response(
                    200,
                    json={"result": {"SetAutoResults": [{"AdGroupId": 20}]}},
                    request=request,
                )
            pages.append(body["params"]["Page"])
            if calls["get"] == 2:
                return httpx.Response(
                    200,
                    json=_keyword_bids_response(limited_by=1),
                    request=request,
                )
            return httpx.Response(200, json=_keyword_bids_response(), request=request)
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    body = {
        "scope": "ad_group",
        "ad_group_ids": [20],
        "rule": {"type": "search_by_traffic_volume", "target_traffic_volume": 75, "bid_ceiling_rub": 12.5},
        "dry_run": False,
        "approved": True,
        "idempotency_key": "set-auto-truncated-ad-group-readback-001",
    }
    try:
        response = client.post(f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/set-auto", json=body)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["applied"] is False
        assert data["partial_failure"] is True
        assert data["readback_failed"] is True
        assert data["readback"] is None
        assert data["verification_error"] == "keywordbids.get readback was truncated after provider write"
        assert pages == [{"Limit": 10_000, "Offset": 0}, {"Limit": 10_000, "Offset": 0}]
        assert calls["setAuto"] == 1
    finally:
        _reset_overrides()
