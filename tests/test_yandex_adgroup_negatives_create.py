from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client, store
from app.yandex_direct import YandexDirectClient

client = TestClient(app)


def _settings(mode: str, token: str | None = "t-secret") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=token)


def _client_with_handler(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


def _override(settings: Settings, yandex_client: YandexDirectClient | None = None):
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex_client


def _clear():
    app.dependency_overrides.clear()


def test_ad_group_negative_keywords_get_maps_live_items():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["method"] == "get"
        assert "NegativeKeywords" in body["params"]["FieldNames"]
        return httpx.Response(
            200,
            json={
                "result": {
                    "AdGroups": [
                        {
                            "Id": 1001,
                            "CampaignId": 2002,
                            "Name": "Group",
                            "Status": "ACCEPTED",
                            "NegativeKeywords": {"Items": ["-old", "spam"]},
                        }
                    ]
                }
            },
        )

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    resp = client.get("/yandex/campaigns/2002/ad-groups/negative-keywords")
    _clear()

    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["ad_group_id"] == "1001"
    assert item["negative_keywords"] == ["old", "spam"]
    assert item["has_negative_keywords"] is True
    assert item["source"] == "yandex"
    assert item["read_only"] is True


def test_negative_keywords_add_dry_run_merges_dedupes_no_update():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body["method"])
        assert body["method"] == "get"
        return httpx.Response(
            200,
            json={
                "result": {
                    "AdGroups": [
                        {"Id": 1001, "CampaignId": 2002, "NegativeKeywords": {"Items": ["old", "bad"]}}
                    ]
                }
            },
        )

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    resp = client.post(
        "/yandex/campaigns/2002/ad-groups/1001/negative-keywords",
        json={
            "approved": True,
            "idempotency_key": "neg-dry-001",
            "dry_run": True,
            "operation": "add",
            "negative_keywords": ["-bad", "new", "new"],
        },
    )
    _clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["negative_keywords"] == ["old", "bad", "new"]
    assert body["payload_preview"]["method"] == "adgroups.update"
    assert calls == ["get"]


def test_negative_keywords_add_live_write_with_pre_read_not_ok_returns_502_no_update_and_no_cache():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body["method"])
        if body["method"] == "get":
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "error": {"error_code": 314, "error_detail": "upstream unavailable"},
                },
            )
        raise AssertionError("adgroups.update must not be called when pre-read is not ok")

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    resp = client.post(
        "/yandex/campaigns/2002/ad-groups/1001/negative-keywords",
        json={
            "approved": True,
            "idempotency_key": "neg-add-get-fail-001",
            "dry_run": False,
            "operation": "add",
            "negative_keywords": ["new"],
        },
    )
    _clear()

    assert resp.status_code == 502, resp.text
    assert calls == ["get"]
    detail = resp.json()["detail"]
    assert detail["error_type"] == "YandexDirectError"
    assert detail["error_code"] == 314
    assert "upstream unavailable" in detail["error_detail"]
    assert "applied" not in resp.json()
    assert "neg-add-get-fail-001" not in store.apply_results_by_key


def test_negative_keywords_live_readonly_apply_is_rejected_before_network():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("network must not be called")

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    resp = client.post(
        "/yandex/campaigns/2002/ad-groups/1001/negative-keywords",
        json={
            "approved": True,
            "idempotency_key": "neg-block-001",
            "dry_run": False,
            "operation": "replace",
            "negative_keywords": ["bad"],
        },
    )
    _clear()

    assert resp.status_code == 409, resp.text
    assert "live_write" in resp.json()["detail"]


def test_campaign_ad_group_create_requires_region_ids():
    _override(_settings("live_write"))
    resp = client.post(
        "/yandex/campaigns/2002/ad-groups",
        json={
            "approved": True,
            "idempotency_key": "grp-reg-001",
            "dry_run": True,
            "name": "New group",
        },
    )
    _clear()

    assert resp.status_code == 422


def test_campaign_ad_group_create_dry_run_reads_geo_but_never_mutates():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body["method"])
        assert request.url.path.endswith("/dictionaries")
        assert body["method"] == "get"
        return httpx.Response(
            200,
            json={"result": {"GeoRegions": [{"GeoRegionId": 213, "GeoRegionName": "Moscow"}]}},
        )

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    resp = client.post(
        "/yandex/campaigns/2002/ad-groups",
        json={
            "approved": True,
            "idempotency_key": "grp-dry-001",
            "dry_run": True,
            "name": "New group",
            "region_ids": [213],
            "negative_keywords": ["-bad"],
        },
    )
    _clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["regions"][0]["name"] == "Moscow"
    preview_group = body["payload_preview"]["params"]["AdGroups"][0]
    assert preview_group["CampaignId"] == 2002
    assert preview_group["RegionIds"] == [213]
    assert preview_group["NegativeKeywords"]["Items"] == ["bad"]
    assert calls == ["get"]


def test_campaign_ad_group_create_live_write_calls_adgroups_add_and_verifies_readback():
    captured: dict[str, Any] = {}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body["method"])
        if request.url.path.endswith("/dictionaries"):
            return httpx.Response(
                200,
                json={"result": {"GeoRegions": [{"GeoRegionId": 213, "GeoRegionName": "Moscow"}]}},
            )
        if body["method"] == "add":
            captured["body"] = body
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
                            "RegionIds": [213],
                        }
                    ]
                }
            },
        )

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    resp = client.post(
        "/yandex/campaigns/2002/ad-groups",
        json={
            "approved": True,
            "idempotency_key": "grp-apply-001",
            "dry_run": False,
            "name": "New group",
            "region_ids": [213],
        },
    )
    _clear()

    assert resp.status_code == 200, resp.text
    assert resp.json()["ad_group_ids"] == [3003]
    assert resp.json()["readback"]["region_ids"] == [213]
    assert captured["body"]["method"] == "add"
    assert captured["body"]["params"]["AdGroups"][0]["Name"] == "New group"
    assert calls == ["get", "add", "get", "get"]


def test_negative_keywords_apply_with_ok_false_returns_502_and_not_applied():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body["method"])
        if body["method"] == "get":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AdGroups": [
                            {"Id": 1001, "CampaignId": 2002, "NegativeKeywords": {"Items": ["old"]}}
                        ]
                    }
                },
            )
        assert body["method"] == "update"
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error": {"error_code": 999, "error_detail": "quota exceeded"},
            },
        )

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    resp = client.post(
        "/yandex/campaigns/2002/ad-groups/1001/negative-keywords",
        json={
            "approved": True,
            "idempotency_key": "neg-apply-fail-001",
            "dry_run": False,
            "operation": "replace",
            "negative_keywords": ["bad"],
        },
    )
    _clear()

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == 999
    assert "quota exceeded" in detail["error_detail"]
    assert "applied" not in resp.json()
    assert calls == ["get", "update"]
    assert "neg-apply-fail-001" not in store.apply_results_by_key


def test_ad_group_create_apply_with_ok_false_returns_502_and_not_applied():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body["method"])
        if request.url.path.endswith("/dictionaries"):
            return httpx.Response(
                200,
                json={"result": {"GeoRegions": [{"GeoRegionId": 213, "GeoRegionName": "Moscow"}]}},
            )
        assert body["method"] == "add"
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error": {"error_code": 111, "error_detail": "invalid name"},
            },
        )

    settings = _settings("live_write")
    _override(settings, _client_with_handler(settings, handler))
    resp = client.post(
        "/yandex/campaigns/2002/ad-groups",
        json={
            "approved": True,
            "idempotency_key": "grp-apply-fail-001",
            "dry_run": False,
            "name": "New group",
            "region_ids": [213],
        },
    )
    _clear()

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == 111
    assert "invalid name" in detail["error_detail"]
    assert "applied" not in resp.json()
    assert calls == ["get", "add"]
    assert "grp-apply-fail-001" not in store._ad_group_create_results_by_key


def test_new_endpoints_are_in_openapi():
    paths = client.get("/openapi.json").json()["paths"]
    assert "/yandex/campaigns/{campaign_id}/ad-groups/negative-keywords" in paths
    assert "/yandex/campaigns/{campaign_id}/ad-groups/{ad_group_id}/negative-keywords" in paths
    assert "/yandex/campaigns/{campaign_id}/ad-groups/{ad_group_id}/geo" in paths
    assert "/yandex/campaigns/{campaign_id}/ad-groups" in paths
