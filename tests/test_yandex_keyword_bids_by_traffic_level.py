"""Contract tests for exact discrete Yandex auction traffic-level bid selection."""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.yandex_direct import YandexDirectClient


CAMPAIGN_ID = "123"
client = TestClient(app)


def _settings(*, mode: str = "live_readonly") -> Settings:
    return Settings(
        _env_file=None,
        directpilot_mode=mode,
        yandex_oauth_token="test-value",
    )


def _direct_client(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


def _reset_overrides() -> None:
    app.dependency_overrides.clear()


def test_preview_uses_only_the_exact_auction_traffic_level_bid_without_write() -> None:
    settings = _settings()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
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
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            assert body["method"] == "get"
            assert body["params"]["SelectionCriteria"] == {"CampaignIds": [123], "Ids": [10]}
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {
                                "Id": 10,
                                "CampaignId": 123,
                                "AdGroupId": 20,
                                "Keyword": "repair dishwasher",
                                "Bid": 300_000_000,
                                "State": "ON",
                                "Status": "ACCEPTED",
                                "ServingStatus": "ELIGIBLE",
                            }
                        ]
                    }
                },
                request=request,
            )
        if "/keywordbids" in str(request.url):
            assert body["method"] == "get", "dry_run must not call keywordbids.set"
            calls.append("keywordbids.get")
            assert body["params"]["SelectionCriteria"] == {
                "CampaignIds": [123],
                "KeywordIds": [10],
            }
            assert "Bid" in body["params"]["SearchFieldNames"]
            assert "AuctionBids" in body["params"]["SearchFieldNames"]
            return httpx.Response(
                200,
                json={
                    "result": {
                        "KeywordBids": [
                            {
                                "KeywordId": 10,
                                "CampaignId": 123,
                                "Search": {
                                    "Bid": 300_000_000,
                                    "AuctionBids": {
                                        "AuctionBidItems": [
                                            {
                                                "TrafficVolume": 75,
                                                "Bid": 250_000_000,
                                                "Price": 0,
                                            },
                                            {
                                                "TrafficVolume": 85,
                                                "Bid": 313_600_000,
                                                "Price": 0,
                                            },
                                            {
                                                "TrafficVolume": 100,
                                                "Bid": 400_000_000,
                                                "Price": 0,
                                            },
                                        ]
                                    },
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
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json={
                "target_traffic_volume": 85,
                "keyword_ids": [10],
                "dry_run": True,
                "approved": True,
                "idempotency_key": "traffic-preview-1",
            },
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    data = response.json()
    assert calls == ["campaigns.get", "keywords.get", "keywordbids.get"]
    assert data["source"] == "yandex"
    assert data["dry_run"] is True
    assert data["applied"] is False
    assert data["target_traffic_volume"] == 85
    assert data["payload_preview"] == {
        "method": "set",
        "params": {"KeywordBids": [{"KeywordId": 10, "SearchBid": 313_600_000}]},
    }
    assert data["items"] == [
        {
            "keyword_id": 10,
            "ad_group_id": 20,
            "phrase": "repair dishwasher",
            "current_search_bid_rub": 300.0,
            "target_traffic_volume": 85,
            "target_bid_rub": 313.6,
            "target_price_rub": 0.0,
            "status": "READY",
            "reason": None,
        }
    ]


def _strategy(search_type: str = "HIGHEST_POSITION") -> dict:
    return {
        "result": {
            "Campaigns": [
                {
                    "Id": 123,
                    "TextCampaign": {
                        "BiddingStrategy": {
                            "Search": {"BiddingStrategyType": search_type}
                        }
                    },
                }
            ]
        }
    }


def _keyword(
    keyword_id: int = 10,
    *,
    phrase: str = "repair dishwasher",
    bid: int = 300_000_000,
    state: str = "ON",
    status: str = "ACCEPTED",
    serving_status: str = "ELIGIBLE",
) -> dict:
    return {
        "Id": keyword_id,
        "CampaignId": 123,
        "AdGroupId": keyword_id + 10,
        "Keyword": phrase,
        "Bid": bid,
        "State": state,
        "Status": status,
        "ServingStatus": serving_status,
    }


def _auction_row(keyword_id: int, levels: list[dict]) -> dict:
    return {
        "KeywordId": keyword_id,
        "CampaignId": 123,
        "Search": {
            "Bid": 300_000_000,
            "AuctionBids": {"AuctionBidItems": levels},
        },
    }


def _request(**overrides) -> dict:
    body = {
        "target_traffic_volume": 85,
        "keyword_ids": [10],
        "dry_run": True,
        "approved": True,
        "idempotency_key": "traffic-level-default",
    }
    body.update(overrides)
    return body


def test_fractional_traffic_level_is_rejected_before_any_provider_call() -> None:
    response = client.post(
        f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
        json=_request(target_traffic_volume=83.86, idempotency_key="traffic-fractional-1"),
    )

    assert response.status_code == 422
    assert "target_traffic_volume" in response.text


def test_boolean_is_not_accepted_as_an_official_integer_traffic_level() -> None:
    response = client.post(
        f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
        json=_request(target_traffic_volume=True, idempotency_key="traffic-bool-level"),
    )

    assert response.status_code == 422
    assert "target_traffic_volume" in response.text


def test_keyword_ids_require_explicit_json_integer_ids_before_provider_reads() -> None:
    response = client.post(
        f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
        json=_request(keyword_ids=["10"], idempotency_key="traffic-string-key-id"),
    )

    assert response.status_code == 422
    assert "keyword_ids" in response.text


def test_missing_exact_level_is_unavailable_without_max_below_interpolation_or_write() -> None:
    settings = _settings(mode="live_write")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            calls.append("campaigns.get")
            return httpx.Response(200, json=_strategy(), request=request)
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            return httpx.Response(200, json={"result": {"Keywords": [_keyword()]}}, request=request)
        if "/keywordbids" in str(request.url):
            assert body["method"] == "get", "missing target level must not reach keywordbids.set"
            calls.append("keywordbids.get")
            return httpx.Response(
                200,
                json={
                    "result": {
                        "KeywordBids": [
                            _auction_row(
                                10,
                                [
                                    {"TrafficVolume": 75, "Bid": 250_000_000, "Price": 0},
                                    {"TrafficVolume": 100, "Bid": 400_000_000, "Price": 0},
                                ],
                            )
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
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(dry_run=False, idempotency_key="traffic-missing-85"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    data = response.json()
    assert calls == ["campaigns.get", "keywords.get", "keywordbids.get"]
    assert data["applied"] is False
    assert data["payload_preview"] == {"method": "set", "params": {"KeywordBids": []}}
    assert data["items"][0]["status"] == "UNAVAILABLE"
    assert data["items"][0]["reason"] == "TARGET_LEVEL_NOT_AVAILABLE"
    assert data["items"][0]["target_bid_rub"] is None


def test_null_documented_auction_data_is_unavailable_and_never_uses_a_fallback_bid() -> None:
    settings = _settings()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy(), request=request)
        if "/keywords" in str(request.url):
            return httpx.Response(200, json={"result": {"Keywords": [_keyword()]}}, request=request)
        if "/keywordbids" in str(request.url):
            assert body["method"] == "get"
            calls.append("keywordbids.get")
            row = _auction_row(10, [])
            row["Search"]["AuctionBids"] = None
            return httpx.Response(
                200,
                json={"result": {"KeywordBids": [row]}},
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(idempotency_key="traffic-null-auction"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    assert calls == ["keywordbids.get"]
    item = response.json()["items"][0]
    assert item["status"] == "UNAVAILABLE"
    assert item["reason"] == "TARGET_LEVEL_NOT_AVAILABLE"
    assert item["target_bid_rub"] is None


def test_autotargeting_is_not_applicable_and_never_enters_auction_or_writer_calls() -> None:
    settings = _settings(mode="live_write")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/campaigns" in str(request.url):
            calls.append("campaigns.get")
            return httpx.Response(200, json=_strategy(), request=request)
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            return httpx.Response(
                200,
                json={"result": {"Keywords": [_keyword(phrase="---autotargeting")]}},
                request=request,
            )
        if "/keywordbids" in str(request.url):
            raise AssertionError("autotargeting must not enter keywordbids.get or keywordbids.set")
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(dry_run=False, idempotency_key="traffic-autotargeting"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    assert calls == ["campaigns.get", "keywords.get"]
    item = response.json()["items"][0]
    assert item["status"] == "NOT_APPLICABLE"
    assert item["reason"] == "AUTOTARGETING"


def test_automatic_strategy_fails_closed_without_auction_or_writer_call() -> None:
    settings = _settings(mode="live_write")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/campaigns" in str(request.url):
            calls.append("campaigns.get")
            return httpx.Response(
                200,
                json=_strategy("WB_MAXIMUM_CONVERSION_RATE"),
                request=request,
            )
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            return httpx.Response(200, json={"result": {"Keywords": [_keyword()]}}, request=request)
        if "/keywordbids" in str(request.url):
            raise AssertionError("automatic strategy must fail closed before auction or writer calls")
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(dry_run=False, idempotency_key="traffic-auto-strategy"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    assert calls == ["campaigns.get", "keywords.get"]
    item = response.json()["items"][0]
    assert item["status"] == "NOT_APPLICABLE"
    assert item["reason"] == "INCOMPATIBLE_CAMPAIGN_STRATEGY"


def test_real_apply_requires_approval_live_write_and_valid_idempotency_key_before_reads() -> None:
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Safety gate leaked a provider call: {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        not_approved = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(approved=False, idempotency_key="traffic-unapproved"),
        )
        non_live_write = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(dry_run=False, idempotency_key="traffic-readonly"),
        )
        invalid_idempotency = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(idempotency_key="bad"),
        )
    finally:
        _reset_overrides()

    assert not_approved.status_code == 409
    assert non_live_write.status_code == 409
    assert invalid_idempotency.status_code == 422


def test_successful_apply_writes_only_ready_target_bids_and_verifies_readback() -> None:
    settings = _settings(mode="live_write")
    calls: list[str] = []
    keyword_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal keyword_reads
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            calls.append("campaigns.get")
            return httpx.Response(200, json=_strategy(), request=request)
        if "/keywords" in str(request.url):
            keyword_reads += 1
            calls.append(f"keywords.get.{keyword_reads}")
            expected_bid = 300_000_000 if keyword_reads == 1 else 313_600_000
            return httpx.Response(
                200,
                json={"result": {"Keywords": [_keyword(bid=expected_bid)]}},
                request=request,
            )
        if "/keywordbids" in str(request.url):
            if body["method"] == "get":
                calls.append("keywordbids.get")
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "KeywordBids": [
                                _auction_row(
                                    10,
                                    [{"TrafficVolume": 85, "Bid": 313_600_000, "Price": 0}],
                                )
                            ]
                        }
                    },
                    request=request,
                )
            calls.append("keywordbids.set")
            assert body == {
                "method": "set",
                "params": {"KeywordBids": [{"KeywordId": 10, "SearchBid": 313_600_000}]},
            }
            return httpx.Response(
                200,
                json={"result": {"SetResults": [{"Id": 10}]}},
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(dry_run=False, idempotency_key="traffic-apply-success"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    data = response.json()
    assert calls == [
        "campaigns.get",
        "keywords.get.1",
        "keywordbids.get",
        "keywordbids.set",
        "keywords.get.2",
    ]
    assert data["applied"] is True
    assert data["items"][0]["status"] == "APPLIED"
    assert data["readback"] == [
        {"keyword_id": 10, "search_bid_micros": 313_600_000, "search_bid_rub": 313.6}
    ]


def test_malformed_per_item_set_result_under_http_200_is_not_reported_as_success() -> None:
    settings = _settings(mode="live_write")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy(), request=request)
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            return httpx.Response(200, json={"result": {"Keywords": [_keyword()]}}, request=request)
        if "/keywordbids" in str(request.url):
            if body["method"] == "get":
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "KeywordBids": [
                                _auction_row(
                                    10,
                                    [{"TrafficVolume": 85, "Bid": 313_600_000, "Price": 0}],
                                )
                            ]
                        }
                    },
                    request=request,
                )
            calls.append("keywordbids.set")
            return httpx.Response(
                200,
                json={"result": {"SetResults": [{"Id": 10}, {"Id": 10}]}},
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(dry_run=False, idempotency_key="traffic-malformed-set"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    data = response.json()
    assert calls == ["keywords.get", "keywordbids.set"]
    assert data["applied"] is False
    assert data["partial_failure"] is True
    assert data["items"][0]["status"] == "FAILED"
    assert data["items"][0]["reason"] == "MALFORMED_SET_RESULT"


def test_readback_mismatch_is_a_safe_failure_without_blind_retry() -> None:
    settings = _settings(mode="live_write")
    keyword_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal keyword_reads
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy(), request=request)
        if "/keywords" in str(request.url):
            keyword_reads += 1
            return httpx.Response(200, json={"result": {"Keywords": [_keyword()]}}, request=request)
        if "/keywordbids" in str(request.url):
            if body["method"] == "get":
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "KeywordBids": [
                                _auction_row(
                                    10,
                                    [{"TrafficVolume": 85, "Bid": 313_600_000, "Price": 0}],
                                )
                            ]
                        }
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                json={"result": {"SetResults": [{"Id": 10}]}},
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(dry_run=False, idempotency_key="traffic-readback-mismatch"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    data = response.json()
    assert keyword_reads == 2
    assert data["applied"] is False
    assert data["items"][0]["status"] == "FAILED"
    assert data["items"][0]["reason"] == "READBACK_MISMATCH"


def test_provider_per_item_error_under_http_200_is_a_safe_failure() -> None:
    settings = _settings(mode="live_write")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy(), request=request)
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            return httpx.Response(200, json={"result": {"Keywords": [_keyword()]}}, request=request)
        if "/keywordbids" in str(request.url):
            if body["method"] == "get":
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "KeywordBids": [
                                _auction_row(
                                    10,
                                    [{"TrafficVolume": 85, "Bid": 313_600_000, "Price": 0}],
                                )
                            ]
                        }
                    },
                    request=request,
                )
            calls.append("keywordbids.set")
            return httpx.Response(
                200,
                json={
                    "result": {
                        "SetResults": [
                            {"Id": 10, "Errors": [{"Code": 52, "Message": "Bid rejected"}]}
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
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(dry_run=False, idempotency_key="traffic-set-item-error"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    data = response.json()
    assert calls == ["keywords.get", "keywordbids.set"]
    assert data["applied"] is False
    assert data["partial_failure"] is True
    assert data["items"][0]["status"] == "FAILED"
    assert data["items"][0]["reason"] == "PROVIDER_SET_ERROR"


def test_inactive_rejected_and_not_serving_keywords_are_excluded_before_auction_reads() -> None:
    settings = _settings(mode="live_write")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/campaigns" in str(request.url):
            calls.append("campaigns.get")
            return httpx.Response(200, json=_strategy(), request=request)
        if "/keywords" in str(request.url):
            calls.append("keywords.get")
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            _keyword(10, state="OFF"),
                            _keyword(11, status="REJECTED"),
                            _keyword(12, serving_status="RARELY_SERVED"),
                        ]
                    }
                },
                request=request,
            )
        if "/keywordbids" in str(request.url):
            raise AssertionError("non-serving keywords must not enter auction or writer calls")
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(
                keyword_ids=[10, 11, 12],
                dry_run=False,
                idempotency_key="traffic-nonserving-keys",
            ),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    assert calls == ["campaigns.get", "keywords.get"]
    assert [item["status"] for item in response.json()["items"]] == [
        "NOT_APPLICABLE",
        "NOT_APPLICABLE",
        "NOT_APPLICABLE",
    ]
    assert [item["reason"] for item in response.json()["items"]] == [
        "KEYWORD_NOT_ELIGIBLE",
        "KEYWORD_NOT_ELIGIBLE",
        "KEYWORD_NOT_ELIGIBLE",
    ]


def test_apply_excludes_nonready_key_and_preserves_writer_payload_to_target_fields_only() -> None:
    settings = _settings(mode="live_write")
    keyword_reads = 0
    writer_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal keyword_reads
        body = json.loads(request.content)
        if "/campaigns" in str(request.url):
            return httpx.Response(200, json=_strategy(), request=request)
        if "/keywords" in str(request.url):
            keyword_reads += 1
            rows = [
                _keyword(10, bid=313_600_000 if keyword_reads > 1 else 300_000_000),
                _keyword(11, phrase="inactive key", state="OFF"),
            ]
            return httpx.Response(200, json={"result": {"Keywords": rows}}, request=request)
        if "/keywordbids" in str(request.url):
            if body["method"] == "get":
                assert body["params"]["SelectionCriteria"]["KeywordIds"] == [10]
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "KeywordBids": [
                                _auction_row(
                                    10,
                                    [{"TrafficVolume": 85, "Bid": 313_600_000, "Price": 0}],
                                )
                            ]
                        }
                    },
                    request=request,
                )
            writer_payloads.append(body)
            return httpx.Response(
                200,
                json={"result": {"SetResults": [{"Id": 10}]}},
                request=request,
            )
        raise AssertionError(f"Unexpected request {request.url}")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _direct_client(settings, handler)
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/keyword-bids/by-traffic-level",
            json=_request(
                keyword_ids=[10, 11],
                dry_run=False,
                idempotency_key="traffic-target-fields-only",
            ),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    assert writer_payloads == [
        {
            "method": "set",
            "params": {"KeywordBids": [{"KeywordId": 10, "SearchBid": 313_600_000}]},
        }
    ]
    items = response.json()["items"]
    assert items[0]["status"] == "APPLIED"
    assert items[1]["status"] == "NOT_APPLICABLE"
    assert items[1]["reason"] == "KEYWORD_NOT_ELIGIBLE"


def test_openapi_exposes_the_discrete_traffic_level_contract() -> None:
    schema = app.openapi()
    operation = schema["paths"][
        "/yandex/campaigns/{campaign_id}/keyword-bids/by-traffic-level"
    ]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    request_schema = schema["components"]["schemas"][request_ref.rsplit("/", 1)[-1]]

    assert operation["summary"] == "Preview/apply Search bids at an exact auction traffic-volume level"
    assert request_schema["properties"]["target_traffic_volume"]["type"] == "integer"
    assert "fractional Direct UI traffic forecast" in request_schema["properties"]["target_traffic_volume"]["description"]
