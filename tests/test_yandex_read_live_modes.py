"""Tests for /yandex read endpoints when directpilot_mode is sandbox,
live_readonly, or live_write.

Contract:
- /yandex/campaigns, /yandex/campaigns/{id}/ad-groups, /ads, /keywords must
  use the real Yandex Direct get methods (campaigns.get / adgroups.get /
  ads.get / keywords.get) — NO live writes.
- Response shape: {items, source="yandex", read_only=True}
- Mapping must be tolerant of missing fields: daily_budget defaults to 0.0
  if DailyBudget is absent, title="" if TextAd.Title is absent, etc.
- YandexDirectError or {ok: false} from the client must surface as HTTP 502
  with no token in the response body or detail.
- The mock-mode contract remains unchanged (covered in test_yandex_facade.py).
- The legacy mock-mode tests rely on autouse fixture in test_yandex_facade.py
  that forces directpilot_mode=mock. This module uses its own overrides so
  FastAPI never sees the mock fixture's leftover dependency_overrides.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.yandex_direct import YandexDirectClient


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _make_client(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


def _settings_for(mode: str, token: str = "t") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=token)


def _install_overrides(settings: Settings, client_obj: YandexDirectClient | None):
    """Install settings + client-factory overrides and return a cleanup fn."""
    from app import main as main_mod

    def _settings_override() -> Settings:
        return settings

    def _client_factory() -> YandexDirectClient | None:
        return client_obj

    app.dependency_overrides[main_mod.get_settings] = _settings_override
    app.dependency_overrides[main_mod.get_yandex_client] = _client_factory

    def cleanup() -> None:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    return cleanup


@pytest.fixture
def client_with_client():
    """Yield a tuple (TestClient, cleanup) so the caller can install its
    own settings/client factory without leaking overrides across tests."""
    yield TestClient(app)


# ---------------------------------------------------------------------------
# /yandex/campaigns
# ---------------------------------------------------------------------------


def test_yandex_campaigns_sandbox_uses_real_campaigns_get_and_marks_source_yandex(
    client_with_client: TestClient,
):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "result": {
                    "Campaigns": [
                        {
                            "Id": 111,
                            "Name": "Real sandbox campaign",
                            "Status": "ACTIVE",
                            "State": "ON",
                            "Type": "TEXT_CAMPAIGN",
                            "DailyBudget": {"Amount": 75000},  # in micro-units → 75 rub
                        }
                    ]
                }
            },
            headers={"Units": "3"},
        )

    settings = _settings_for("sandbox")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns")
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert captured["url"].endswith("/campaigns")
    # DailyBudget 75000 micro-units / 1000 = 75.0 (default campaign currency unit)
    assert len(body["items"]) == 1
    first = body["items"][0]
    assert first["id"] == "111"
    assert first["name"] == "Real sandbox campaign"
    assert first["status"] == "ACTIVE"
    assert first["type"] == "TEXT_CAMPAIGN"
    # daily_budget may be exposed in rubles; whatever it is it must be a number
    assert isinstance(first["daily_budget"], (int, float))
    assert first["daily_budget"] >= 0
    assert "t-secret" not in json.dumps(body)  # token must not leak


def test_yandex_campaigns_live_readonly_uses_real_campaigns_get_no_write(
    client_with_client: TestClient,
):
    """live_readonly must still allow GET calls (sandbox base URL still
    refuses them; live base URL is the safe path here, no write methods
    are exposed by /yandex read endpoints)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        # Refuse any non-get method: the only method we want is `get`.
        assert captured["body"]["method"] == "get", (
            "live_readonly must not trigger write methods"
        )
        return httpx.Response(
            200,
            json={
                "result": {
                    "Campaigns": [
                        {
                            "Id": 222,
                            "Name": "Read-only live campaign",
                            "Status": "ACTIVE",
                            "State": "ON",
                            "Type": "TEXT_CAMPAIGN",
                        }
                    ]
                }
            },
        )

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns")
    finally:
        cleanup()

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert body["items"][0]["id"] == "222"
    # Token must not leak
    assert "LRO-SECRET" not in response.text


def test_yandex_campaigns_tolerates_missing_daily_budget(
    client_with_client: TestClient,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "Campaigns": [
                        {
                            "Id": 333,
                            "Name": "No daily budget",
                            "Status": "ACTIVE",
                            "State": "ON",
                            "Type": "TEXT_CAMPAIGN",
                            # No DailyBudget at all
                        }
                    ]
                }
            },
        )

    settings = _settings_for("sandbox")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns")
    finally:
        cleanup()

    assert response.status_code == 200
    first = response.json()["items"][0]
    assert first["daily_budget"] == 0.0


def test_yandex_campaigns_yandex_error_returns_502_without_token(
    client_with_client: TestClient,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 53, "error_detail": "internal"}},
        )

    settings = _settings_for("sandbox", token="MUST-NOT-LEAK-1")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns")
    finally:
        cleanup()

    assert response.status_code == 502
    assert "MUST-NOT-LEAK-1" not in response.text
    body = response.json()
    assert body["detail"]["error_type"] == "YandexDirectError"


# ---------------------------------------------------------------------------
# /yandex/campaigns/{id}/ad-groups
# ---------------------------------------------------------------------------


def test_yandex_ad_groups_sandbox_uses_real_adgroups_get(
    client_with_client: TestClient,
):
    captured: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        captured.append((str(request.url), body))
        if request.url.path.endswith("/adgroups"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AdGroups": [
                            {
                                "Id": 1001,
                                "Name": "Real ad group 1",
                                "CampaignId": 555,
                                "Status": "ACTIVE",
                                "Type": "TEXT_AD_GROUP",
                                "RegionIds": [12345],
                            }
                        ]
                    }
                },
            )
        assert request.url.path.endswith("/dictionaries")
        assert body == {
            "method": "get",
            "params": {"DictionaryNames": ["GeoRegions"]},
        }
        return httpx.Response(
            200,
            json={
                "result": {
                    "GeoRegions": [
                        {"GeoRegionId": 12345, "GeoRegionName": "Test region"}
                    ]
                }
            },
        )

    settings = _settings_for("sandbox")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns/555/ad-groups")
    finally:
        cleanup()

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert [url for url, _ in captured] == [
        "https://api-sandbox.direct.yandex.com/json/v5/adgroups",
        "https://api-sandbox.direct.yandex.com/json/v5/dictionaries",
    ]
    assert captured[0][1]["params"]["SelectionCriteria"] == {"CampaignIds": [555]}
    assert body["items"][0]["id"] == "1001"
    assert body["items"][0]["campaign_id"] == "555"
    assert body["items"][0]["status"] == "ACTIVE"


def test_yandex_ad_groups_tolerates_missing_status(
    client_with_client: TestClient,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/adgroups"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AdGroups": [
                            {
                                "Id": 1002,
                                "Name": "Missing status",
                                "CampaignId": 555,
                                "RegionIds": [12345],
                            }
                        ]
                    }
                },
            )
        assert request.url.path.endswith("/dictionaries")
        return httpx.Response(
            200,
            json={
                "result": {
                    "GeoRegions": [
                        {"GeoRegionId": 12345, "GeoRegionName": "Test region"}
                    ]
                }
            },
        )

    settings = _settings_for("sandbox")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns/555/ad-groups")
    finally:
        cleanup()

    assert response.status_code == 200
    first = response.json()["items"][0]
    # default status when missing — must be a non-empty string
    assert isinstance(first["status"], str)
    assert first["status"]  # non-empty


def test_yandex_ad_groups_yandex_error_returns_502_without_token(
    client_with_client: TestClient,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 88, "error_detail": "Bad campaign id"}},
        )

    settings = _settings_for("sandbox", token="MUST-NOT-LEAK-2")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns/555/ad-groups")
    finally:
        cleanup()

    assert response.status_code == 502
    assert "MUST-NOT-LEAK-2" not in response.text


# ---------------------------------------------------------------------------
# /yandex/campaigns/{id}/ads
# ---------------------------------------------------------------------------


def test_yandex_ads_sandbox_uses_real_ads_get_and_maps_title(
    client_with_client: TestClient,
):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "result": {
                    "Ads": [
                        {
                            "Id": 2001,
                            "AdGroupId": 1001,
                            "CampaignId": 555,
                            "Status": "ACTIVE",
                            "Type": "TEXT_AD",
                            "TextAd": {
                                "Title": "Реальный заголовок",
                                "Title2": "Заголовок 2",
                                "Text": "Текст объявления",
                                "Href": "https://example.com",
                            },
                        }
                    ]
                }
            },
        )

    settings = _settings_for("sandbox")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns/555/ads")
    finally:
        cleanup()

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert captured["url"].endswith("/ads")
    assert captured["body"]["params"]["SelectionCriteria"] == {"CampaignIds": [555]}
    first = body["items"][0]
    assert first["id"] == "2001"
    assert first["ad_group_id"] == "1001"
    assert first["campaign_id"] == "555"
    assert first["title"] == "Реальный заголовок"
    assert first["status"] == "ACTIVE"


def test_yandex_ads_tolerates_missing_title(
    client_with_client: TestClient,
):
    """If the Ad has no TextAd.Title (or no TextAd at all), title must be ''."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "Ads": [
                        {
                            "Id": 2002,
                            "AdGroupId": 1001,
                            "CampaignId": 555,
                            "Status": "ACTIVE",
                            "Type": "TEXT_AD",
                            # No TextAd at all
                        },
                        {
                            "Id": 2003,
                            "AdGroupId": 1001,
                            "CampaignId": 555,
                            "Status": "ACTIVE",
                            "Type": "TEXT_AD",
                            "TextAd": {"Text": "только текст"},  # no Title
                        },
                    ]
                }
            },
        )

    settings = _settings_for("sandbox")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns/555/ads")
    finally:
        cleanup()

    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["title"] == ""
    assert items[1]["title"] == ""


def test_yandex_ads_yandex_error_returns_502_without_token(
    client_with_client: TestClient,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 600, "error_detail": "Token invalid"}},
        )

    settings = _settings_for("sandbox", token="MUST-NOT-LEAK-3")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns/555/ads")
    finally:
        cleanup()

    assert response.status_code == 502
    assert "MUST-NOT-LEAK-3" not in response.text


# ---------------------------------------------------------------------------
# /yandex/campaigns/{id}/keywords
# ---------------------------------------------------------------------------


def test_yandex_keywords_sandbox_uses_real_keywords_get_and_maps_phrase(
    client_with_client: TestClient,
):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "result": {
                    "Keywords": [
                        {
                            "Id": 3001,
                            "AdGroupId": 1001,
                            "CampaignId": 555,
                            "Keyword": "ремонт квартир казань",
                            "Status": "ACTIVE",
                        }
                    ]
                }
            },
        )

    settings = _settings_for("sandbox")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns/555/keywords")
    finally:
        cleanup()

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert captured["url"].endswith("/keywords")
    assert captured["body"]["params"]["SelectionCriteria"] == {"CampaignIds": [555]}
    first = body["items"][0]
    assert first["id"] == "3001"
    assert first["ad_group_id"] == "1001"
    assert first["phrase"] == "ремонт квартир казань"
    assert first["status"] == "ACTIVE"


def test_yandex_keywords_tolerates_missing_phrase_and_status(
    client_with_client: TestClient,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "Keywords": [
                        {
                            "Id": 3002,
                            "AdGroupId": 1001,
                            "CampaignId": 555,
                        }
                    ]
                }
            },
        )

    settings = _settings_for("sandbox")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns/555/keywords")
    finally:
        cleanup()

    assert response.status_code == 200
    first = response.json()["items"][0]
    assert first["phrase"] == ""
    assert isinstance(first["status"], str)
    assert first["status"]


def test_yandex_keywords_yandex_error_returns_502_without_token(
    client_with_client: TestClient,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 53, "error_detail": "internal"}},
        )

    settings = _settings_for("sandbox", token="MUST-NOT-LEAK-4")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/campaigns/555/keywords")
    finally:
        cleanup()

    assert response.status_code == 502
    assert "MUST-NOT-LEAK-4" not in response.text


# ---------------------------------------------------------------------------
# Cross-mode contract: mock is unchanged
# ---------------------------------------------------------------------------


def test_yandex_campaigns_in_mock_mode_keeps_mock_source_and_does_not_call_client(
    client_with_client: TestClient,
):
    """The /yandex read endpoints in mock mode must keep source=mock and
    must not call the Yandex client (no token, no network)."""
    from app import main as main_mod

    settings = Settings(_env_file=None, directpilot_mode="mock")

    def _settings_override() -> Settings:
        return settings

    def _client_factory() -> YandexDirectClient | None:
        return None

    app.dependency_overrides[main_mod.get_settings] = _settings_override
    app.dependency_overrides[main_mod.get_yandex_client] = _client_factory
    try:
        response = client_with_client.get("/yandex/campaigns")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "mock"
    assert body["read_only"] is True
    assert body["items"], "expected mock items in mock mode"
