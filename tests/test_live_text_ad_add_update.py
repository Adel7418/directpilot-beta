"""Safety contract tests for live TextAd add and PATCH routes.

All provider interactions use httpx.MockTransport; these tests never contact
Yandex Direct.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.yandex_direct import YandexDirectClient

client = TestClient(app)


def _settings(mode: str) -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token="test-token")


def _client_with_handler(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


def _override(settings: Settings, yandex_client: YandexDirectClient | None = None) -> None:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex_client


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def test_text_ad_add_preview_reuses_only_explicitly_selected_parent_assets() -> None:
    """A typed parent ID permits safe reuse without template guessing or mutation."""
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        requests.append(body)
        assert body["method"] == "get"
        assert body["params"]["SelectionCriteria"]["Ids"] == [42]
        return httpx.Response(
            200,
            json={
                "result": {
                    "Ads": [
                        {
                            "Id": 42,
                            "Type": "TEXT_AD",
                            "AdGroupId": 1001,
                            "Status": "DRAFT",
                            "TextAd": {
                                "Title": "Parent title",
                                "Title2": "Parent title 2",
                                "Text": "Parent text",
                                "Href": "https://example.test/offer?utm_source=parent&utm_medium=cpc#anchor",
                                "BusinessId": 777,
                                "SitelinkSetId": 888,
                                "PreferVCardOverBusiness": "NO",
                            },
                        }
                    ]
                }
            },
        )

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.post(
            "/yandex/ad-groups/1001/ads",
            json={
                "approved": True,
                "idempotency_key": "inherit-preview-001",
                "dry_run": True,
                "ads": [
                    {
                        "inherit_from_ad_id": 42,
                        "title": "New explicit title",
                        "text": "New explicit text",
                    }
                ],
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is False
    assert len(requests) == 1  # Read-only parent lookup; never ads.add.

    preview = body["payload_preview"]
    text_ad = preview["params"]["Ads"][0]["TextAd"]
    assert text_ad == {
        "Title": "New explicit title",
        "Title2": "Parent title 2",
        "Text": "New explicit text",
        "Href": "https://example.test/offer?utm_source=parent&utm_medium=cpc#anchor",
        "BusinessId": 777,
        "SitelinkSetId": 888,
        "PreferVCardOverBusiness": "NO",
        "Mobile": "NO",
    }
    fields = preview["inheritance"][0]["fields"]
    assert fields["Title"]["source"] == "explicit"
    assert fields["Text"]["source"] == "explicit"
    assert fields["Href"]["source"] == "reused"
    assert fields["UTM"] == {
        "source": "reused",
        "value": {"utm_source": "parent", "utm_medium": "cpc"},
    }
    assert fields["BusinessId"]["source"] == "reused"
    assert fields["SitelinkSetId"]["source"] == "reused"
    assert fields["PreferVCardOverBusiness"]["source"] == "reused"


def test_text_ad_add_apply_fails_closed_when_detailed_readback_mismatches() -> None:
    """A successful AddResults item is insufficient without factual matching readback."""
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        methods.append(body["method"])
        if body["method"] == "add":
            return httpx.Response(200, json={"result": {"AddResults": [{"Id": 501}]}})
        assert body["params"]["SelectionCriteria"]["Ids"] == [501]
        return httpx.Response(
            200,
            json={
                "result": {
                    "Ads": [
                        {
                            "Id": 501,
                            "Type": "TEXT_AD",
                            "AdGroupId": 1001,
                            "Status": "DRAFT",
                            "TextAd": {
                                "Title": "Expected title",
                                "Text": "Expected text",
                                "Href": "https://example.test/path?utm_source=changed",
                                "BusinessId": 777,
                                "SitelinkSetId": 888,
                                "PreferVCardOverBusiness": "NO",
                                "Mobile": "NO",
                            },
                        }
                    ]
                }
            },
        )

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.post(
            "/yandex/ad-groups/1001/ads",
            json={
                "approved": True,
                "idempotency_key": "add-readback-mismatch-001",
                "dry_run": False,
                "ads": [
                    {
                        "title": "Expected title",
                        "text": "Expected text",
                        "href": "https://example.test/path?utm_source=expected#fragment",
                        "business_id": 777,
                        "sitelink_set_id": 888,
                        "prefer_vcard_over_business": "NO",
                    }
                ],
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 502, response.text
    assert methods == ["add", "get"]


def test_text_ad_patch_preview_reads_actual_ad_and_sends_only_requested_field() -> None:
    """Text-only PATCH never merges or nulls unrelated link/contact fields."""
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        methods.append(body["method"])
        assert body["method"] == "get"
        assert body["params"]["SelectionCriteria"]["Ids"] == [42]
        return httpx.Response(
            200,
            json={
                "result": {
                    "Ads": [
                        {
                            "Id": 42,
                            "Type": "TEXT_AD",
                            "AdGroupId": 1001,
                            "Status": "DRAFT",
                            "TextAd": {
                                "Title": "Old title",
                                "Title2": "Old title 2",
                                "Text": "Old body",
                                "DisplayUrlPath": "old/path",
                                "Href": "https://example.test/path?utm_source=old#fragment",
                                "BusinessId": 777,
                                "SitelinkSetId": 888,
                                "PreferVCardOverBusiness": "NO",
                                "Mobile": "NO",
                            },
                        }
                    ]
                }
            },
        )

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.patch(
            "/yandex/ads/42",
            json={
                "approved": True,
                "idempotency_key": "patch-preview-001",
                "dry_run": True,
                "title": "New title",
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is False
    assert methods == ["get"]
    assert body["before_after"] == {"Title": {"before": "Old title", "after": "New title"}}
    sent_text_ad = body["payload_preview"]["params"]["Ads"][0]["TextAd"]
    assert sent_text_ad == {"Title": "New title"}
    assert "Href" not in sent_text_ad
    assert "BusinessId" not in sent_text_ad
    assert "SitelinkSetId" not in sent_text_ad
    assert body["moderation"] == {
        "route": "/yandex/ads/moderate",
        "status": "not_requested",
    }


def test_text_ad_patch_apply_uses_minimal_payload_and_preserves_unrequested_assets() -> None:
    """PATCH applies only requested Href/BusinessId and verifies untouched assets."""
    requests: list[dict[str, Any]] = []
    reads = 0

    def ad_payload(*, href: str, business_id: int) -> dict[str, Any]:
        return {
            "Id": 42,
            "Type": "TEXT_AD",
            "AdGroupId": 1001,
            "Status": "DRAFT",
            "TextAd": {
                "Title": "Old title",
                "Title2": "Old title 2",
                "Text": "Old body",
                "DisplayUrlPath": "old/path",
                "Href": href,
                "BusinessId": business_id,
                "SitelinkSetId": 888,
                "PreferVCardOverBusiness": "NO",
                "Mobile": "NO",
            },
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        body = json.loads(request.content.decode())
        requests.append(body)
        if body["method"] == "get":
            reads += 1
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Ads": [
                            ad_payload(
                                href=(
                                    "https://example.test/path?utm_source=old#fragment"
                                    if reads == 1
                                    else "https://example.test/new?utm_source=new#new-fragment"
                                ),
                                business_id=777 if reads == 1 else 999,
                            )
                        ]
                    }
                },
            )
        assert body == {
            "method": "update",
            "params": {
                "Ads": [
                    {
                        "Id": 42,
                        "TextAd": {
                            "Href": "https://example.test/new?utm_source=new#new-fragment",
                            "BusinessId": 999,
                        },
                    }
                ]
            },
        }
        return httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 42}]}})

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.patch(
            "/yandex/ads/42",
            json={
                "approved": True,
                "idempotency_key": "patch-apply-001",
                "dry_run": False,
                "href": "https://example.test/new?utm_source=new#new-fragment",
                "business_id": 999,
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert [request["method"] for request in requests] == ["get", "update", "get"]
    assert body["before_after"]["Href"] == {
        "before": "https://example.test/path?utm_source=old#fragment",
        "after": "https://example.test/new?utm_source=new#new-fragment",
    }
    assert body["before_after"]["UTM"] == {
        "before": {"utm_source": "old"},
        "after": {"utm_source": "new"},
    }
    assert body["readback"]["TextAd"]["SitelinkSetId"] == 888
    assert body["readback"]["TextAd"]["PreferVCardOverBusiness"] == "NO"
    assert body["moderation"]["status"] == "not_requested"


def test_text_ad_add_rejects_non_numeric_ad_group_id_before_preview() -> None:
    """Official TextAdAdd always needs numeric AdGroupId; omit is not a safe fallback."""
    _override(_settings("live_write"))
    try:
        response = client.post(
            "/yandex/ad-groups/not-a-direct-id/ads",
            json={
                "approved": True,
                "idempotency_key": "invalid-ad-group-001",
                "dry_run": True,
                "ads": [
                    {
                        "title": "Title",
                        "text": "Text",
                        "href": "https://example.test/path",
                    }
                ],
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 409, response.text
    assert "numeric" in response.json()["detail"]


def test_text_ad_add_inherits_turbo_page_as_destination_for_sitelinks() -> None:
    """TurboPageId is an official destination and permits inherited sitelinks."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["method"] == "get"
        return httpx.Response(
            200,
            json={
                "result": {
                    "Ads": [
                        {
                            "Id": 84,
                            "Type": "TEXT_AD",
                            "AdGroupId": 1001,
                            "Status": "DRAFT",
                            "TextAd": {
                                "Title": "Parent title",
                                "Text": "Parent text",
                                "TurboPageId": 90210,
                                "SitelinkSetId": 888,
                            },
                        }
                    ]
                }
            },
        )

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.post(
            "/yandex/ad-groups/1001/ads",
            json={
                "approved": True,
                "idempotency_key": "inherit-turbo-001",
                "dry_run": True,
                "ads": [{"inherit_from_ad_id": 84}],
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    text_ad = response.json()["payload_preview"]["params"]["Ads"][0]["TextAd"]
    assert text_ad["TurboPageId"] == 90210
    assert text_ad["SitelinkSetId"] == 888
    assert "Href" not in text_ad


def test_text_ad_add_does_not_guess_parent_when_destination_is_omitted() -> None:
    """No inherit_from_ad_id means no group/template lookup fallback is allowed."""
    provider_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_calls.append(json.loads(request.content.decode()))
        raise AssertionError("no provider call is valid without an explicit parent")

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.post(
            "/yandex/ad-groups/1001/ads",
            json={
                "approved": True,
                "idempotency_key": "no-template-guess-001",
                "dry_run": True,
                "ads": [{"title": "Title", "text": "Text"}],
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 502, response.text
    assert provider_calls == []


def test_text_ad_add_fails_closed_on_any_item_error_without_readback() -> None:
    """Partial AddResults must never be surfaced as a successful partial create."""
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        methods.append(body["method"])
        return httpx.Response(
            200,
            json={
                "result": {
                    "AddResults": [
                        {"Id": 501},
                        {"Errors": [{"Code": 123, "Message": "invalid"}]},
                    ]
                }
            },
        )

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.post(
            "/yandex/ad-groups/1001/ads",
            json={
                "approved": True,
                "idempotency_key": "add-item-error-001",
                "dry_run": False,
                "ads": [
                    {"title": "First", "text": "First body", "href": "https://example.test/one"},
                    {"title": "Second", "text": "Second body", "href": "https://example.test/two"},
                ],
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 502, response.text
    assert methods == ["add"]


def test_text_ad_add_rejects_reused_idempotency_key_for_changed_payload() -> None:
    """A changed TextAd payload cannot receive a stale cached successful result."""
    methods: list[str] = []
    added_text_ad: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        methods.append(body["method"])
        if body["method"] == "add":
            added_text_ad.update(body["params"]["Ads"][0]["TextAd"])
            return httpx.Response(200, json={"result": {"AddResults": [{"Id": 501}]}})
        return httpx.Response(
            200,
            json={
                "result": {
                    "Ads": [
                        {
                            "Id": 501,
                            "Type": "TEXT_AD",
                            "AdGroupId": 1001,
                            "Status": "DRAFT",
                            "TextAd": added_text_ad,
                        }
                    ]
                }
            },
        )

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    try:
        first = client.post(
            "/yandex/ad-groups/1001/ads",
            json={
                "approved": True,
                "idempotency_key": "add-idempotency-fingerprint-001",
                "dry_run": False,
                "ads": [{"title": "Original", "text": "Body", "href": "https://example.test"}],
            },
        )
        changed = client.post(
            "/yandex/ad-groups/1001/ads",
            json={
                "approved": True,
                "idempotency_key": "add-idempotency-fingerprint-001",
                "dry_run": False,
                "ads": [{"title": "Changed", "text": "Body", "href": "https://example.test"}],
            },
        )
    finally:
        _clear_overrides()

    assert first.status_code == 200, first.text
    assert changed.status_code == 409, changed.text
    assert methods == ["add", "get"]
