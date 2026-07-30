from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.store import store
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


def _adgroup_response(region_ids: list[int], *, name: str = "Existing group") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "result": {
                "AdGroups": [
                    {
                        "Id": 1001,
                        "CampaignId": 2002,
                        "Name": name,
                        "Status": "ACCEPTED",
                        "RegionIds": region_ids,
                    }
                ]
            }
        },
    )


def _dictionary_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "result": {
                "GeoRegions": [
                    {"GeoRegionId": 123, "GeoRegionName": "Target"},
                    {"GeoRegionId": 213, "GeoRegionName": "Previous"},
                    {"GeoRegionId": 456, "GeoRegionName": "Different"},
                    {"GeoRegionId": 999, "GeoRegionName": "Mismatch"},
                ]
            }
        },
    )


@pytest.mark.parametrize(
    ("mode", "approved"),
    [("live_readonly", True), ("live_write", False)],
)
def test_ad_group_geo_apply_gates_block_before_any_provider_call(mode: str, approved: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"provider must not be called: {request.url}")

    settings = _settings(mode)
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups/1001/geo",
            json={
                "approved": approved,
                "idempotency_key": f"geo-gate-{mode}",
                "dry_run": False,
                "region_ids": [123],
            },
        )
    finally:
        _clear()

    assert response.status_code == 409, response.text


@pytest.mark.parametrize("region_ids", [[0, -123], [-123]])
def test_ad_group_geo_rejects_invalid_complete_targets_before_provider_read(region_ids: list[int]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"provider must not be called: {request.url}")

    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups/1001/geo",
            json={
                "idempotency_key": f"geo-invalid-{len(region_ids)}-{region_ids[0]}",
                "dry_run": True,
                "region_ids": region_ids,
            },
        )
    finally:
        _clear()

    assert response.status_code == 422, response.text


def test_ad_group_geo_pre_read_provider_failure_fails_closed_without_update_or_cache() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body["method"])
        assert body["method"] == "get"
        return httpx.Response(200, json={"ok": False, "error": {"error_code": 503}})

    key = "geo-preread-fail-001"
    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups/1001/geo",
            json={
                "approved": True,
                "idempotency_key": key,
                "dry_run": False,
                "region_ids": [123],
            },
        )
    finally:
        _clear()

    assert response.status_code == 502, response.text
    assert calls == ["get"]
    assert key not in store._ad_group_geo_results_by_key


def test_ad_group_geo_update_item_errors_fail_closed_without_readback_or_cache() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body["method"])
        if request.url.path.endswith("/dictionaries"):
            return _dictionary_response()
        if body["method"] == "get":
            return _adgroup_response([213])
        assert body["method"] == "update"
        return httpx.Response(
            200,
            json={
                "result": {
                    "UpdateResults": [
                        {"Id": 1001, "Errors": [{"Code": 1, "Message": "rejected"}]}
                    ]
                }
            },
        )

    key = "geo-item-error-001"
    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups/1001/geo",
            json={
                "approved": True,
                "idempotency_key": key,
                "dry_run": False,
                "region_ids": [123],
            },
        )
    finally:
        _clear()

    assert response.status_code == 502, response.text
    assert calls == ["get", "get", "update"]
    assert key not in store._ad_group_geo_results_by_key


def test_ad_group_geo_readback_mismatch_fails_closed_without_cache() -> None:
    calls: list[str] = []
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body["method"])
        if request.url.path.endswith("/dictionaries"):
            return _dictionary_response()
        if body["method"] == "get":
            get_count += 1
            return _adgroup_response([213] if get_count == 1 else [123, 123])
        assert body["method"] == "update"
        return httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 1001}]}})

    key = "geo-readback-mismatch-001"
    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups/1001/geo",
            json={
                "approved": True,
                "idempotency_key": key,
                "dry_run": False,
                "region_ids": [123],
            },
        )
    finally:
        _clear()

    assert response.status_code == 502, response.text
    assert calls == ["get", "get", "update", "get", "get"]
    assert key not in store._ad_group_geo_results_by_key


def test_ad_group_geo_idempotency_replays_matching_request_and_rejects_different_payload() -> None:
    calls: list[str] = []
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body["method"])
        if request.url.path.endswith("/dictionaries"):
            return _dictionary_response()
        if body["method"] == "get":
            get_count += 1
            return _adgroup_response([213] if get_count == 1 else [123])
        assert body["method"] == "update"
        return httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 1001}]}})

    key = "geo-idempotency-001"
    request = {
        "approved": True,
        "idempotency_key": key,
        "dry_run": False,
        "region_ids": [123],
    }
    settings = _settings()
    _override(settings, handler)
    try:
        initial = client.post("/yandex/campaigns/2002/ad-groups/1001/geo", json=request)
        calls_after_initial = list(calls)
        replay = client.post("/yandex/campaigns/2002/ad-groups/1001/geo", json=request)
        conflict = client.post(
            "/yandex/campaigns/2002/ad-groups/1001/geo",
            json={**request, "region_ids": [456]},
        )
    finally:
        _clear()

    assert initial.status_code == 200, initial.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == initial.json()
    assert conflict.status_code == 409, conflict.text
    assert calls_after_initial == ["get", "get", "update", "get", "get"]
    assert calls == calls_after_initial


def test_ad_group_create_item_errors_fail_closed_without_readback_or_cache() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body["method"])
        if request.url.path.endswith("/dictionaries"):
            return _dictionary_response()
        assert body["method"] == "add"
        return httpx.Response(
            200,
            json={"result": {"AddResults": [{"Errors": [{"Code": 1, "Message": "rejected"}]}]}},
        )

    key = "create-item-error-001"
    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups",
            json={
                "approved": True,
                "idempotency_key": key,
                "dry_run": False,
                "name": "New group",
                "region_ids": [123],
            },
        )
    finally:
        _clear()

    assert response.status_code == 502, response.text
    assert calls == ["get", "add"]
    assert key not in store._ad_group_create_results_by_key


def test_ad_group_create_readback_mismatch_fails_closed_without_cache() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body["method"])
        if request.url.path.endswith("/dictionaries"):
            return _dictionary_response()
        if body["method"] == "add":
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
                            "RegionIds": [123, 123],
                        }
                    ]
                }
            },
        )

    key = "create-readback-mismatch-001"
    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups",
            json={
                "approved": True,
                "idempotency_key": key,
                "dry_run": False,
                "name": "New group",
                "region_ids": [123],
            },
        )
    finally:
        _clear()

    assert response.status_code == 502, response.text
    assert calls == ["get", "add", "get"]
    assert key not in store._ad_group_create_results_by_key


def test_ad_group_create_idempotency_replays_matching_request_and_rejects_different_payload() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body["method"])
        if request.url.path.endswith("/dictionaries"):
            return _dictionary_response()
        if body["method"] == "add":
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
                            "RegionIds": [123],
                        }
                    ]
                }
            },
        )

    key = "create-idempotency-001"
    request = {
        "approved": True,
        "idempotency_key": key,
        "dry_run": False,
        "name": "New group",
        "region_ids": [123],
    }
    settings = _settings()
    _override(settings, handler)
    try:
        initial = client.post("/yandex/campaigns/2002/ad-groups", json=request)
        calls_after_initial = list(calls)
        replay = client.post("/yandex/campaigns/2002/ad-groups", json=request)
        conflict = client.post(
            "/yandex/campaigns/2002/ad-groups",
            json={**request, "name": "Different group"},
        )
    finally:
        _clear()

    assert initial.status_code == 200, initial.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == initial.json()
    assert conflict.status_code == 409, conflict.text
    assert calls_after_initial == ["get", "add", "get", "get"]
    assert calls == calls_after_initial


@pytest.mark.parametrize(
    ("geo_regions", "expected_status"),
    [
        (
            [
                {
                    "GeoRegionId": 123,
                    "GeoRegionName": "Different",
                    "ParentGeoRegionNames": {"Items": []},
                }
            ],
            404,
        ),
        (
            [
                {
                    "GeoRegionId": 123,
                    "GeoRegionName": "Target",
                    "ParentGeoRegionNames": {"Items": []},
                },
                {
                    "GeoRegionId": 456,
                    "GeoRegionName": "Target",
                    "ParentGeoRegionNames": {"Items": []},
                },
            ],
            409,
        ),
    ],
)
def test_ad_group_geo_name_resolution_fails_closed_before_group_read(
    geo_regions: list[dict[str, Any]], expected_status: int
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(body["method"])
        assert request.url.path.endswith("/dictionaries")
        assert body["method"] == "getGeoRegions"
        return httpx.Response(200, json={"result": {"GeoRegions": geo_regions}})

    settings = _settings()
    _override(settings, handler)
    try:
        response = client.post(
            "/yandex/campaigns/2002/ad-groups/1001/geo",
            json={
                "idempotency_key": f"geo-name-fail-{expected_status}",
                "dry_run": True,
                "region_names": ["Target"],
            },
        )
    finally:
        _clear()

    assert response.status_code == expected_status, response.text
    assert calls == ["getGeoRegions"]


def test_geo_regions_get_by_name_uses_exact_names_selection_criteria() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"result": {"GeoRegions": []}})

    direct = YandexDirectClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )

    response = direct.geo_regions_get_by_name("Target")

    assert response["ok"] is True
    assert len(requests) == 1
    assert requests[0]["method"] == "getGeoRegions"
    assert requests[0]["params"]["SelectionCriteria"] == {"ExactNames": ["Target"]}
    assert "Names" not in requests[0]["params"]["SelectionCriteria"]
