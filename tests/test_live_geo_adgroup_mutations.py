from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.yandex_direct import YandexDirectClient

client = TestClient(app)


def _settings(mode: str = "live_write") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token="test-token")


def _override(settings: Settings, handler) -> None:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: YandexDirectClient(
        settings=settings, transport=httpx.MockTransport(handler)
    )


def _clear() -> None:
    app.dependency_overrides.clear()


def test_ad_group_geo_dry_run_reads_live_state_previews_complete_geo_only_update_without_mutation() -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body)
        if request.url.path.endswith("/adgroups"):
            assert body["method"] == "get"
            assert "RegionIds" in body["params"]["FieldNames"]
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AdGroups": [
                            {
                                "Id": 1001,
                                "CampaignId": 2002,
                                "Name": "Existing group",
                                "Status": "ACCEPTED",
                                "RegionIds": [213],
                            }
                        ]
                    }
                },
            )
        if request.url.path.endswith("/dictionaries"):
            assert body["method"] == "get"
            return httpx.Response(
                200,
                json={
                    "result": {
                        "GeoRegions": [
                            {"GeoRegionId": 213, "GeoRegionName": "Moscow"},
                            {"GeoRegionId": 12345, "GeoRegionName": "Zelenodolsk"},
                        ]
                    }
                },
            )
        raise AssertionError(f"unexpected provider mutation: {body}")

    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups/1001/geo",
            json={
                "approved": False,
                "idempotency_key": "geo-dry-001",
                "dry_run": True,
                "region_ids": [12345],
            },
        )
    finally:
        _clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["source"] == "yandex"
    assert body["scope"] == "ad_group"
    assert body["before"]["region_ids"] == [213]
    assert body["before"]["regions"][0]["name"] == "Moscow"
    assert body["after"]["region_ids"] == [12345]
    assert body["after"]["regions"][0]["name"] == "Zelenodolsk"
    assert [region["region_id"] for region in body["added"]] == [12345]
    assert [region["region_id"] for region in body["removed"]] == [213]
    assert body["payload_preview"] == {
        "method": "adgroups.update",
        "params": {"AdGroups": [{"Id": 1001, "RegionIds": [12345]}]},
    }
    assert body["risk_warning"] == "Changing ad-group geo can change reach and spend."
    assert body["preserved_entities"] == [
        "negative_keywords",
        "keywords",
        "ads",
        "strategy",
        "budget",
        "goals",
        "links_utm_assets",
    ]
    assert [call["method"] for call in calls] == ["get", "get"]


def test_ad_group_geo_apply_sends_only_complete_geo_payload_and_returns_verified_readback() -> None:
    calls: list[dict[str, Any]] = []
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body)
        if request.url.path.endswith("/adgroups") and body["method"] == "get":
            get_count += 1
            region_ids = [213] if get_count == 1 else [12345]
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AdGroups": [
                            {
                                "Id": 1001,
                                "CampaignId": 2002,
                                "Name": "Existing group",
                                "Status": "ACCEPTED",
                                "RegionIds": region_ids,
                                "NegativeKeywords": {"Items": ["must-not-be-sent"]},
                                "Strategy": {"Type": "ignored"},
                            }
                        ]
                    }
                },
            )
        if request.url.path.endswith("/dictionaries"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "GeoRegions": [
                            {"GeoRegionId": 213, "GeoRegionName": "Moscow"},
                            {"GeoRegionId": 12345, "GeoRegionName": "Zelenodolsk"},
                        ]
                    }
                },
            )
        assert request.url.path.endswith("/adgroups")
        assert body == {
            "method": "update",
            "params": {"AdGroups": [{"Id": 1001, "RegionIds": [12345]}]},
        }
        return httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 1001}]}})

    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups/1001/geo",
            json={
                "approved": True,
                "idempotency_key": "geo-apply-001",
                "dry_run": False,
                "region_ids": [12345],
            },
        )
    finally:
        _clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["readback"]["region_ids"] == [12345]
    assert body["after"]["region_ids"] == [12345]
    assert [call["method"] for call in calls] == ["get", "get", "update", "get", "get"]


def test_ad_group_geo_dry_run_resolves_authoritative_region_names() -> None:
    calls: list[dict[str, Any]] = []
    dictionary_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body)
        if request.url.path.endswith("/dictionaries"):
            dictionary_calls.append(body)
            assert body == {
                "method": "get",
                "params": {"DictionaryNames": ["GeoRegions"]},
            }
            return httpx.Response(
                200,
                json={
                    "result": {
                        "GeoRegions": [
                            {"GeoRegionId": 213, "GeoRegionName": "Moscow"},
                            {
                                "GeoRegionId": 12345,
                                "GeoRegionName": "Zelenodolsk",
                                "ParentGeoRegionNames": {"Items": ["Russia"]},
                            }
                        ]
                    }
                },
            )
        if request.url.path.endswith("/adgroups"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AdGroups": [
                            {
                                "Id": 1001,
                                "CampaignId": 2002,
                                "Name": "Existing group",
                                "Status": "ACCEPTED",
                                "RegionIds": [213],
                            }
                        ]
                    }
                },
            )
        raise AssertionError(f"unexpected upstream request: {request.url.path}")

    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups/1001/geo",
            json={
                "idempotency_key": "geo-names-001",
                "dry_run": True,
                "region_names": ["Zelenodolsk"],
            },
        )
    finally:
        _clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["after"]["region_ids"] == [12345]
    assert body["after"]["regions"][0]["name"] == "Zelenodolsk"
    assert [call["method"] for call in calls] == ["get", "get", "get"]
    assert dictionary_calls == [
        {"method": "get", "params": {"DictionaryNames": ["GeoRegions"]}}
    ] * 2


def test_ad_group_create_resolves_names_previews_canonical_geo_and_returns_verified_readback() -> None:
    calls: list[dict[str, Any]] = []
    dictionary_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body)
        if request.url.path.endswith("/dictionaries"):
            dictionary_calls.append(body)
            assert body == {
                "method": "get",
                "params": {"DictionaryNames": ["GeoRegions"]},
            }
            return httpx.Response(
                200,
                json={
                    "result": {
                        "GeoRegions": [
                            {
                                "GeoRegionId": 12345,
                                "GeoRegionName": "Zelenodolsk",
                            }
                        ]
                    }
                },
            )
        if body["method"] == "add":
            assert body == {
                "method": "add",
                "params": {
                    "AdGroups": [
                        {
                            "Name": "New group",
                            "CampaignId": 2002,
                            "RegionIds": [12345],
                            "NegativeKeywords": {"Items": ["bad"]},
                        }
                    ]
                },
            }
            return httpx.Response(200, json={"result": {"AddResults": [{"Id": 3003}]}})
        assert body["method"] == "get"
        return httpx.Response(
            200,
            json={
                "result": {
                    "AdGroups": [
                        {
                            "Id": 3003,
                            "CampaignId": 2002,
                            "Name": "New group",
                            "Status": "DRAFT",
                            "RegionIds": [12345],
                            "NegativeKeywords": {"Items": ["bad"]},
                        }
                    ]
                }
            },
        )

    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups",
            json={
                "approved": True,
                "idempotency_key": "create-names-001",
                "dry_run": False,
                "name": "New group",
                "region_names": ["Zelenodolsk"],
                "negative_keywords": ["-bad"],
            },
        )
    finally:
        _clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["ad_group_ids"] == [3003]
    assert body["regions"][0]["region_id"] == 12345
    assert body["regions"][0]["name"] == "Zelenodolsk"
    assert body["readback"] == {
        "ad_group_id": 3003,
        "campaign_id": "2002",
        "name": "New group",
        "region_ids": [12345],
        "negative_keywords": ["bad"],
        "regions": [
            {
                "region_id": 12345,
                "name": "Zelenodolsk",
                "type": None,
                "parent_id": None,
                "parent_name": None,
                "parent_names": [],
                "dictionary_region_id": 12345,
                "excluded": False,
                "all_regions": False,
                "dictionary_resolved": True,
                "source": "yandex",
                "read_only": True,
            }
        ],
    }
    assert [call["method"] for call in calls] == ["get", "get", "add", "get", "get"]
    assert dictionary_calls == [
        {"method": "get", "params": {"DictionaryNames": ["GeoRegions"]}}
    ] * 3


def test_ad_group_geo_apply_accepts_readback_region_ids_in_different_signed_order() -> None:
    calls: list[str] = []
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body["method"])
        if request.url.path.endswith("/adgroups") and body["method"] == "get":
            get_count += 1
            region_ids = [213] if get_count == 1 else [12345, -456]
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AdGroups": [
                            {
                                "Id": 1001,
                                "CampaignId": 2002,
                                "Name": "Existing group",
                                "Status": "ACCEPTED",
                                "RegionIds": region_ids,
                            }
                        ]
                    }
                },
            )
        if request.url.path.endswith("/dictionaries"):
            assert body["method"] == "get"
            return httpx.Response(
                200,
                json={
                    "result": {
                        "GeoRegions": [
                            {"GeoRegionId": 213, "GeoRegionName": "Moscow"},
                            {"GeoRegionId": 456, "GeoRegionName": "Excluded"},
                            {"GeoRegionId": 12345, "GeoRegionName": "Zelenodolsk"},
                        ]
                    }
                },
            )
        assert request.url.path.endswith("/adgroups")
        assert body == {
            "method": "update",
            "params": {"AdGroups": [{"Id": 1001, "RegionIds": [-456, 12345]}]},
        }
        return httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 1001}]}})

    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups/1001/geo",
            json={
                "approved": True,
                "idempotency_key": "geo-ordered-readback-001",
                "dry_run": False,
                "region_ids": [12345, -456],
            },
        )
    finally:
        _clear()

    assert response.status_code == 200, response.text
    assert response.json()["readback"]["region_ids"] == [12345, -456]
    assert calls == ["get", "get", "update", "get", "get"]


def test_ad_group_create_accepts_readback_region_ids_in_different_signed_order() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body["method"])
        if request.url.path.endswith("/dictionaries"):
            assert body["method"] == "get"
            return httpx.Response(
                200,
                json={
                    "result": {
                        "GeoRegions": [
                            {"GeoRegionId": 456, "GeoRegionName": "Excluded"},
                            {"GeoRegionId": 12345, "GeoRegionName": "Zelenodolsk"},
                        ]
                    }
                },
            )
        if body["method"] == "add":
            assert body["params"]["AdGroups"] == [
                {
                    "Name": "New group",
                    "CampaignId": 2002,
                    "RegionIds": [-456, 12345],
                }
            ]
            return httpx.Response(200, json={"result": {"AddResults": [{"Id": 3003}]}})
        assert body["method"] == "get"
        return httpx.Response(
            200,
            json={
                "result": {
                    "AdGroups": [
                        {
                            "Id": 3003,
                            "CampaignId": 2002,
                            "Name": "New group",
                            "Status": "DRAFT",
                            "RegionIds": [12345, -456],
                        }
                    ]
                }
            },
        )

    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups",
            json={
                "approved": True,
                "idempotency_key": "create-ordered-readback-001",
                "dry_run": False,
                "name": "New group",
                "region_ids": [12345, -456],
            },
        )
    finally:
        _clear()

    assert response.status_code == 200, response.text
    assert response.json()["readback"]["region_ids"] == [12345, -456]
    assert calls == ["get", "add", "get", "get"]
