from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.yandex_direct import YandexDirectClient

client = TestClient(app)


def _settings(mode: str) -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token="test-token")


def _client_with_handler(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


def _override(settings: Settings, yandex_client: YandexDirectClient | None) -> None:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex_client


def _clear() -> None:
    app.dependency_overrides.clear()


def test_campaign_ad_groups_uses_full_dictionary_for_readback_id_to_name_mapping() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(request.url.path)
        if request.url.path.endswith("/adgroups"):
            assert "RegionIds" in body["params"]["FieldNames"]
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AdGroups": [
                            {
                                "Id": 1001,
                                "CampaignId": 2002,
                                "Name": "Зеленодольск",
                                "Status": "ACCEPTED",
                                "RegionIds": [12345],
                            }
                        ]
                    }
                },
            )
        if request.url.path.endswith("/dictionaries"):
            # Full dictionaries.get is intentionally retained only for ad-group
            # RegionId -> name readback and the public resolver share
            # dictionaries.get GeoRegions in live-readonly mode.
            assert body["method"] == "get"
            assert body["params"]["DictionaryNames"] == ["GeoRegions"]
            return httpx.Response(
                200,
                json={
                    "result": {
                        "GeoRegions": [
                            {
                                "GeoRegionId": 12345,
                                "GeoRegionName": "Зеленодольск",
                                "GeoRegionType": "CITY",
                                "ParentId": 43,
                                "ParentName": "Республика Татарстан",
                            }
                        ]
                    }
                },
            )
        raise AssertionError(f"unexpected upstream path: {request.url.path}")

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.get("/yandex/campaigns/2002/ad-groups")
    finally:
        _clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert body["scope"] == "ad_group_region_ids_for_requested_campaign"
    group = body["items"][0]
    assert group["region_ids"] == [12345]
    assert group["geo_scope"] == "ad_group"
    assert group["regions"] == [
        {
            "region_id": 12345,
            "name": "Зеленодольск",
            "type": "CITY",
            "parent_id": 43,
            "parent_name": "Республика Татарстан",
            "parent_names": [],
            "dictionary_region_id": 12345,
            "excluded": False,
            "all_regions": False,
            "dictionary_resolved": True,
            "source": "yandex",
            "read_only": True,
        }
    ]
    assert calls == ["/json/v5/adgroups", "/json/v5/dictionaries"]


@pytest.mark.parametrize(
    ("name", "expected_id", "expected_name"),
    [
        ("  зЕЛЕНОДОЛЬСК  ", 11125, "Зеленодольск"),
        ("Зеленодольский район", 99762, "Зеленодольский район"),
        ("Высокогорский район", 99758, "Высокогорский район"),
        ("Пестречинский район", 99775, "Пестречинский район"),
        ("Лаишевский район", 99766, "Лаишевский район"),
    ],
)
def test_region_resolver_uses_observed_live_dictionary_shape(
    name: str, expected_id: int, expected_name: str
) -> None:
    calls: list[str] = []

    # Sanitized live-readonly observation: dictionaries.get returns
    # result.GeoRegions as a flat array. The specialized getGeoRegions request
    # instead returns result={} for these public names.
    observed_geo_regions = [
        {"GeoRegionId": 99758, "GeoRegionName": "Высокогорский район"},
        {"GeoRegionId": 99762, "GeoRegionName": "Зеленодольский район"},
        {"GeoRegionId": 99766, "GeoRegionName": "Лаишевский район"},
        {"GeoRegionId": 99775, "GeoRegionName": "Пестречинский район"},
        {"GeoRegionId": 11125, "GeoRegionName": "Зеленодольск"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(request.url.path)
        assert request.url.path.endswith("/dictionaries")
        if body["method"] == "getGeoRegions":
            return httpx.Response(200, json={"result": {}})
        assert body == {
            "method": "get",
            "params": {"DictionaryNames": ["GeoRegions"]},
        }
        return httpx.Response(200, json={"result": {"GeoRegions": observed_geo_regions}})

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.get("/yandex/regions/resolve", params={"name": name})
    finally:
        _clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert body["scope"] == "yandex_geo_regions_dictionary"
    assert body["match"] == "exact_normalized_name"
    assert body["region"]["region_id"] == expected_id
    assert body["region"]["name"] == expected_name
    assert body["region"]["parent_names"] == []
    assert body["region"]["excluded"] is False
    assert body["region"]["all_regions"] is False
    assert body["region"]["dictionary_resolved"] is True
    assert calls == ["/json/v5/dictionaries"]


def test_region_resolver_uses_name_compatibility_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public route must exercise the same safe name resolver as mutations."""
    settings = _settings("live_readonly")
    direct = _client_with_handler(
        settings,
        lambda request: (_ for _ in ()).throw(AssertionError(f"unexpected request: {request.url}")),
    )
    resolved_names: list[str] = []

    def geo_regions_get_by_name(name: str) -> dict[str, Any]:
        resolved_names.append(name)
        return {
            "ok": True,
            "result": {
                "GeoRegions": [{"GeoRegionId": 12345, "GeoRegionName": "Target"}]
            },
        }

    def geo_regions_get() -> dict[str, Any]:
        return {"ok": True, "result": {"GeoRegions": []}}

    monkeypatch.setattr(direct, "geo_regions_get_by_name", geo_regions_get_by_name)
    monkeypatch.setattr(direct, "geo_regions_get", geo_regions_get)
    _override(settings, direct)
    try:
        response = client.get("/yandex/regions/resolve", params={"name": "Target"})
    finally:
        _clear()

    assert response.status_code == 200, response.text
    assert resolved_names == ["Target"]


def test_region_resolver_allows_absent_optional_parent_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
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
                        {
                            "GeoRegionId": 12345,
                            "GeoRegionName": "Target",
                        }
                    ]
                }
            },
        )

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.get("/yandex/regions/resolve", params={"name": "Target"})
    finally:
        _clear()

    assert response.status_code == 200, response.text
    assert response.json()["region"]["parent_names"] == []


def test_region_resolver_fails_closed_for_malformed_present_parent_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/dictionaries")
        return httpx.Response(
            200,
            json={
                "result": {
                    "GeoRegions": [
                        {
                            "GeoRegionId": 12345,
                            "GeoRegionName": "Target",
                            "ParentGeoRegionNames": "malformed",
                        }
                    ]
                }
            },
        )

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.get("/yandex/regions/resolve", params={"name": "Target"})
    finally:
        _clear()

    assert response.status_code == 502, response.text
    assert response.json()["detail"]["error_type"] == "YandexDirectError"


@pytest.mark.parametrize(
    "upstream",
    [
        {"result": {}},
        {"result": {"GeoRegions": {}}},
    ],
)
def test_region_resolver_fails_closed_for_incomplete_dictionary_shape(
    upstream: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        assert request.url.path.endswith("/dictionaries")
        assert body == {
            "method": "get",
            "params": {"DictionaryNames": ["GeoRegions"]},
        }
        return httpx.Response(200, json=upstream)

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.get("/yandex/regions/resolve", params={"name": "Target"})
    finally:
        _clear()

    assert response.status_code == 502, response.text
    assert response.json()["detail"]["error_type"] == "YandexDirectError"


def test_region_resolver_fails_closed_for_unknown_name_without_echoing_input() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
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
                        {
                            "GeoRegionId": 12345,
                            "GeoRegionName": "Зеленодольск",
                            "ParentGeoRegionNames": {"Items": []},
                        }
                    ]
                }
            },
        )

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.get("/yandex/regions/resolve", params={"name": "unknown-input-should-not-echo"})
    finally:
        _clear()

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail == {
        "error_type": "YandexRegionResolutionError",
        "message": "No exact Yandex GeoRegions match was found.",
        "reason": "unknown",
    }
    assert "unknown-input-should-not-echo" not in response.text


def test_region_resolver_fails_closed_for_ambiguous_exact_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/dictionaries")
        return httpx.Response(
            200,
            json={
                "result": {
                    "GeoRegions": [
                        {
                            "GeoRegionId": 100,
                            "GeoRegionName": "Зеленодольск",
                            "ParentGeoRegionNames": {"Items": []},
                        },
                        {
                            "GeoRegionId": 200,
                            "GeoRegionName": "Зеленодольск",
                            "ParentGeoRegionNames": {"Items": []},
                        },
                    ]
                }
            },
        )

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.get("/yandex/regions/resolve", params={"name": "Зеленодольск"})
    finally:
        _clear()

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "error_type": "YandexRegionResolutionError",
        "message": "Yandex GeoRegions returned more than one exact match.",
        "reason": "ambiguous",
    }


def test_region_resolver_returns_safe_error_for_provider_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/dictionaries")
        return httpx.Response(
            200,
            json={
                "error": {
                    "error_code": 503,
                    "error_detail": "provider rejected request with secret-never-disclose",
                }
            },
        )

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.get("/yandex/regions/resolve", params={"name": "Зеленодольск"})
    finally:
        _clear()

    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert detail["error_type"] == "YandexDirectError"
    assert "error_code=503" in detail["message"]
    assert "secret-never-disclose" not in response.text


@pytest.mark.parametrize("mode", ["sandbox", "live_readonly", "live_write"])
def test_region_resolver_never_falls_back_to_mock_in_live_modes(mode: str) -> None:
    settings = _settings(mode)
    _override(settings, None)
    try:
        response = client.get("/yandex/regions/resolve", params={"name": "Зеленодольск"})
    finally:
        _clear()

    assert response.status_code == 409, response.text
    assert "mock" not in response.text


def test_campaign_ad_groups_marks_negative_region_id_as_excluded() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode())
        calls.append(request.url.path)
        if request.url.path.endswith("/adgroups"):
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
                                "RegionIds": [-12345],
                            }
                        ]
                    }
                },
            )
        assert request.url.path.endswith("/dictionaries")
        assert body["method"] == "get"
        assert body["params"]["DictionaryNames"] == ["GeoRegions"]
        return httpx.Response(
            200,
            json={
                "result": {
                    "GeoRegions": [
                        {"GeoRegionId": 12345, "GeoRegionName": "Зеленодольск"}
                    ]
                }
            },
        )

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.get("/yandex/campaigns/2002/ad-groups")
    finally:
        _clear()

    assert response.status_code == 200, response.text
    group = response.json()["items"][0]
    assert group["region_ids"] == [-12345]
    assert group["regions"][0]["region_id"] == -12345
    assert group["regions"][0]["dictionary_region_id"] == 12345
    assert group["regions"][0]["name"] == "Зеленодольск"
    assert group["regions"][0]["excluded"] is True
    assert group["regions"][0]["all_regions"] is False
    assert calls == ["/json/v5/adgroups", "/json/v5/dictionaries"]


def test_campaign_ad_groups_represents_zero_region_id_as_all_regions_without_dictionary() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path.endswith("/adgroups")
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
                            "RegionIds": [0],
                        }
                    ]
                }
            },
        )

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.get("/yandex/campaigns/2002/ad-groups")
    finally:
        _clear()

    assert response.status_code == 200, response.text
    group = response.json()["items"][0]
    assert group["region_ids"] == [0]
    assert group["regions"] == [
        {
            "region_id": 0,
            "name": "All regions",
            "type": None,
            "parent_id": None,
            "parent_name": None,
            "parent_names": [],
            "dictionary_region_id": None,
            "excluded": False,
            "all_regions": True,
            "dictionary_resolved": False,
            "source": "yandex",
            "read_only": True,
        }
    ]
    assert calls == ["/json/v5/adgroups"]


def test_campaign_ad_groups_rejects_incomplete_geo_dictionary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/adgroups"):
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
                                "RegionIds": [12345],
                            }
                        ]
                    }
                },
            )
        assert request.url.path.endswith("/dictionaries")
        return httpx.Response(200, json={"result": {"GeoRegions": []}})

    settings = _settings("live_readonly")
    _override(settings, _client_with_handler(settings, handler))
    try:
        response = client.get("/yandex/campaigns/2002/ad-groups")
    finally:
        _clear()

    assert response.status_code == 502, response.text
    assert response.json()["detail"]["error_type"] == "YandexDirectError"
    assert "GeoRegions dictionary did not contain every requested ad-group RegionId" in response.text
