"""Tests for sitelinks fix and campaign ad-assets read endpoint.

Covers:
- sitelinks_get constructs valid v5 request with FieldNames and optional SelectionCriteria
- sitelinks_get handles Direct errors safely (error_code surfaced, no token leak)
- GET /yandex/campaigns/{campaign_id}/ad-assets returns structured assets
- ad-assets works in mock/live_readonly/live_write modes (no writes)
- ads detail includes SitelinkSetId and resolves sitelinks
- OpenAPI includes the new endpoint
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.yandex_direct import YandexDirectClient, YandexDirectError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(mode: str = "live_readonly", token: str | None = "t-secret") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=token)


def _capture_client(settings: Settings) -> tuple[dict, YandexDirectClient]:
    """Build a client that captures the last request body/url."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"result": {"SitelinksSets": []}},
            headers={"Units": "5"},
        )

    client = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    return captured, client


def _make_client(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# sitelinks_get — client method
# ---------------------------------------------------------------------------


def test_sitelinks_get_omits_selection_criteria_when_no_ids_and_includes_field_names():
    """When no ids are passed, SelectionCriteria must be omitted.
    FieldNames must include Id; SitelinkFieldNames requests nested links.
    include Title, Href, Description.
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"result": {"SitelinksSets": [{"Id": 1, "Sitelinks": [{"Title": "Link", "Href": "https://example.com"}]}]}},
        )

    settings = _settings()
    client = _make_client(settings, handler)
    result = client.sitelinks_get()

    assert result["ok"] is True
    body = captured["body"]
    assert body["method"] == "get"
    # SelectionCriteria must be absent (not empty dict) when no filter
    assert "SelectionCriteria" not in body["params"]
    assert "Id" in body["params"]["FieldNames"]
    assert "Sitelinks" not in body["params"]["FieldNames"]
    assert "Title" in body["params"]["SitelinkFieldNames"]
    assert "Href" in body["params"]["SitelinkFieldNames"]
    assert "Description" in body["params"]["SitelinkFieldNames"]
    assert "t-secret" not in str(result)


def test_sitelinks_get_with_ids_sends_selection_criteria_ids():
    """When specific ids are passed, SelectionCriteria.Ids must be sent."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"SitelinksSets": []}})

    settings = _settings()
    client = _make_client(settings, handler)
    client.sitelinks_get(ids=[100, 200])

    body = captured["body"]
    assert body["params"]["SelectionCriteria"]["Ids"] == [100, 200]


def test_sitelinks_get_with_pagination_sends_page_limit_offset():
    """When limit/offset are passed, they must appear in Page."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"SitelinksSets": []}})

    settings = _settings()
    client = _make_client(settings, handler)
    client.sitelinks_get(limit=50, offset=10)

    body = captured["body"]
    assert body["params"]["Page"] == {"Limit": 50, "Offset": 10}


def test_sitelinks_get_surfaces_yandex_error_with_code_and_no_token():
    """A Direct API error must surface with error_code and no token leak."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 8000, "error_detail": "Bad request shape", "error_string": "Invalid argument"}},
        )

    settings = _settings(token="SUPERTOKEN")
    client = _make_client(settings, handler)

    result = client.sitelinks_get()

    assert result["ok"] is False
    assert result["error"]["error_code"] == 8000
    assert result["error"]["error_detail"] == "Bad request shape"
    assert result["error"]["error_string"] == "Invalid argument"
    assert "SUPERTOKEN" not in str(result)


def test_sitelinks_get_missing_token_raises_before_network():
    """Missing token must raise YandexDirectError before any network call."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be reached")

    settings = Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token=None)
    client = _make_client(settings, handler)

    with pytest.raises(YandexDirectError) as exc:
        client.sitelinks_get()
    assert "YANDEX_OAUTH_TOKEN" in str(exc.value)


# ---------------------------------------------------------------------------
# ads_get_detailed — client method for extended TextAd fields
# ---------------------------------------------------------------------------


def test_ads_get_detailed_includes_extended_text_ad_fields():
    """ads_get_detailed must request additional TextAd fields:
    Title2, DisplayUrlPath, SitelinkSetId, BusinessId, VCardId,
    PreferVCardOverBusiness, AdExtensions.
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"Ads": []}})

    settings = _settings()
    client = _make_client(settings, handler)
    client.ads_get_detailed(710382063)

    body = captured["body"]
    # Standard fields still present
    assert "Id" in body["params"]["FieldNames"]
    assert "Type" in body["params"]["FieldNames"]
    assert "Status" in body["params"]["FieldNames"]
    assert "State" in body["params"]["FieldNames"]
    # Extended TextAd fields
    tad_fields = body["params"]["TextAdFieldNames"]
    for f in ("Title", "Title2", "Text", "Href", "DisplayUrlPath",
              "SitelinkSetId", "BusinessId", "VCardId",
              "PreferVCardOverBusiness", "AdExtensions"):
        assert f in tad_fields, f"Missing {f} in TextAdFieldNames"
    # Campaign filter
    assert body["params"]["SelectionCriteria"]["CampaignIds"] == [710382063]


# ---------------------------------------------------------------------------
# sotelinks_get — endpoint
# ---------------------------------------------------------------------------


def test_sitelinks_endpoint_returns_yandex_raw_result_with_source_and_read_only():
    """GET /yandex/sitelinks must return YandexRawResult with source=yandex."""
    from app import main as main_mod

    settings = _settings()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"result": {"SitelinksSets": [{"Id": 1, "Sitelinks": [{"Title": "L", "Href": "https://x.com"}]}]}},
        )

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj
    try:
        response = TestClient(app).get("/yandex/sitelinks")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert body["service"] == "sitelinks"
    assert body["method"] == "get"
    assert body["data"] is not None
    assert "t-secret" not in response.text


def test_sitelinks_endpoint_with_ids_passes_them_through():
    """GET /yandex/sitelinks?ids=10&ids=20 must forward ids to client."""
    from app import main as main_mod

    settings = _settings()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"SitelinksSets": []}})

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj
    try:
        response = TestClient(app).get("/yandex/sitelinks?ids=10&ids=20")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200, response.text
    assert captured["body"]["params"]["SelectionCriteria"]["Ids"] == [10, 20]


def test_sitelinks_endpoint_error_is_surfaced_with_redacted_diagnostics():
    """When Direct rejects sitelinks.get, the 502 must include error_code
    and error_detail but not the token."""
    from app import main as main_mod

    settings = _settings(token="leaked-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 8000, "error_detail": "Bad shape", "error_string": "Invalid argument"}},
        )

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj
    try:
        response = TestClient(app).get("/yandex/sitelinks")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert detail["error_type"] == "YandexDirectError"
    assert "8000" in detail["message"]
    assert "leaked-token" not in response.text


# ---------------------------------------------------------------------------
# GET /yandex/campaigns/{campaign_id}/ad-assets — endpoint
# ---------------------------------------------------------------------------


def test_ad_assets_endpoint_mock_mode_returns_structured_response():
    """In mock mode (no Yandex client), the endpoint returns mock data
    with the expected structure including campaign_id, ads, and empty
    sitelinks/businesses/callouts sections.
    """
    from app import main as main_mod

    settings = Settings(_env_file=None, directpilot_mode="mock")
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: None
    try:
        response = TestClient(app).get("/yandex/campaigns/710382063/ad-assets")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign_id"] == "710382063"
    assert body["source"] == "mock"
    assert body["read_only"] is True
    # Must have ads structure
    assert "ads" in body
    assert isinstance(body["ads"], list)
    # Each ad must have the expected fields
    if body["ads"]:
        ad = body["ads"][0]
        for f in ("id", "ad_group_id", "status", "title", "text", "href"):
            assert f in ad, f"Missing field {f} in ad"
    # Sitelinks section present
    assert "sitelinks_sets" in body
    assert isinstance(body["sitelinks_sets"], list)
    # Businesses section present
    assert "businesses" in body
    # Callouts section present
    assert "callouts" in body
    assert "missing" in body
    assert "callouts" in body["missing"]


def test_ad_assets_endpoint_live_readonly_aggregates_ads_and_sitelinks():
    """In live_readonly, the endpoint must call ads_get_detailed,
    resolve sitelinks for referenced SitelinkSetIds, and return
    structured data without performing any writes.
    """
    from app import main as main_mod

    settings = _settings(token="t-aa")
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append({"url": str(request.url), "body": body})

        service = request.url.path.split("/json/v5/")[-1] if "/json/v5/" in request.url.path else ""
        if service == "ads":
            return httpx.Response(200, json={
                "result": {"Ads": [
                    {"Id": 1, "AdGroupId": 100, "CampaignId": 710382063, "Status": "ACCEPTED", "State": "ON",
                     "Type": "TEXT_AD",
                     "TextAd": {"Title": "Заголовок", "Title2": "Заголовок 2", "Text": "Текст",
                                "Href": "https://example.com", "SitelinkSetId": 42,
                                "BusinessId": 11588384335, "VCardId": 999,
                                "PreferVCardOverBusiness": "YES",
                                "AdExtensions": [1, 2]}},
                ]}
            })
        if service == "sitelinks":
            return httpx.Response(200, json={
                "result": {"SitelinksSets": [
                    {"Id": 42, "Sitelinks": [
                        {"Title": "Быстрая ссылка", "Href": "https://example.com/link1", "Description": "Описание"}
                    ]}
                ]}
            })
        if service == "businesses":
            return httpx.Response(200, json={
                "result": {"Businesses": [
                    {"Id": 11588384335, "Name": "Мой бизнес", "Address": "Москва"}
                ]}
            })
        if service == "vcards":
            return httpx.Response(200, json={
                "result": {"VCards": [
                    {"Id": 999, "CompanyName": "Моя компания", "Phone": {"CountryCode": "7", "CityCode": "495", "PhoneNumber": "1234567"}}
                ]}
            })
        return httpx.Response(200, json={"result": {}})

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj
    try:
        response = TestClient(app).get("/yandex/campaigns/710382063/ad-assets")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign_id"] == "710382063"
    assert body["source"] == "yandex"
    assert body["read_only"] is True

    # Ads present
    assert len(body["ads"]) == 1
    ad = body["ads"][0]
    assert ad["id"] == "1"
    assert ad["title"] == "Заголовок"
    assert ad["title2"] == "Заголовок 2"
    assert ad["text"] == "Текст"
    assert ad["href"] == "https://example.com"
    assert ad["sitelink_set_id"] == "42"
    assert ad["business_id"] == "11588384335"
    assert ad["vcard_id"] == "999"
    assert ad["prefer_vcard_over_business"] == "YES"
    assert ad["ad_extension_ids"] == [1, 2]

    # Sitelinks resolved
    assert len(body["sitelinks_sets"]) == 1
    sl = body["sitelinks_sets"][0]
    assert sl["id"] == "42"
    assert len(sl["sitelinks"]) == 1
    assert sl["sitelinks"][0]["title"] == "Быстрая ссылка"

    # Businesses present
    assert len(body["businesses"]) == 1
    assert body["businesses"][0]["id"] == "11588384335"

    # Vcards present
    assert len(body["vcards"]) == 1
    assert body["vcards"][0]["id"] == "999"

    # Callouts not implemented
    assert body["callouts"] == []
    assert "not_implemented" in body["missing"]["callouts"].lower()

    # No token leak
    assert "t-aa" not in response.text

    # No write calls made
    for call in calls:
        method = call["body"].get("method")
        assert method in ("get", None), f"Unexpected write method: {method} in {call}"


def test_ad_assets_endpoint_no_token_returns_409():
    """Without a Yandex client in live mode, the endpoint returns 409."""
    from app import main as main_mod

    settings = Settings(_env_file=None, directpilot_mode="live_readonly")
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: None
    try:
        response = TestClient(app).get("/yandex/campaigns/710382063/ad-assets")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 409, response.text


def test_ad_assets_sitelinks_error_is_graceful():
    """When sitelinks.get fails, the endpoint must still return ads
    and mark sitelinks as errored rather than crashing."""
    from app import main as main_mod

    settings = _settings(token="t-aa")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        service = request.url.path.split("/json/v5/")[-1] if "/json/v5/" in request.url.path else ""
        if service == "ads":
            return httpx.Response(200, json={
                "result": {"Ads": [
                    {"Id": 1, "AdGroupId": 100, "CampaignId": 710382063, "Status": "ACCEPTED", "State": "ON",
                     "Type": "TEXT_AD",
                     "TextAd": {"Title": "Ad", "Text": "Body", "Href": "https://x.com", "SitelinkSetId": 42}},
                ]}
            })
        if service == "sitelinks":
            return httpx.Response(200, json={"error": {"error_code": 8000, "error_detail": "Bad shape"}})
        return httpx.Response(200, json={"result": {}})

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj
    try:
        response = TestClient(app).get("/yandex/campaigns/710382063/ad-assets")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["ads"]) == 1
    # Sitelinks errored but endpoint did not crash; unresolved sets are omitted.
    assert body["sitelinks_sets"] == []


def test_ad_assets_endpoint_respects_read_only_no_writes():
    """Even in live_write mode, the ad-assets endpoint must NOT perform
    any write operations — only get methods."""
    from app import main as main_mod

    settings = _settings(mode="live_write", token="t-aa")
    write_called = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        method = body.get("method", "")
        if method in ("add", "update", "delete", "archive", "unarchive", "suspend", "resume", "moderate"):
            write_called["count"] += 1
            return httpx.Response(200, json={"result": {}})
        if body.get("method") == "get":
            service = request.url.path.split("/json/v5/")[-1] if "/json/v5/" in request.url.path else ""
            if service == "ads":
                return httpx.Response(200, json={"result": {"Ads": []}})
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(200, json={"result": {}})

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj
    try:
        response = TestClient(app).get("/yandex/campaigns/710382063/ad-assets")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200, response.text
    assert write_called["count"] == 0, "Write methods were called on a read-only endpoint"
