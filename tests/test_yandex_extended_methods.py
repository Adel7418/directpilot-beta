from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.yandex_direct import YandexDirectClient


def _settings(mode: str = "live_readonly") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token="SECRET-TOKEN")


def _client(handler, mode: str = "live_readonly") -> YandexDirectClient:
    return YandexDirectClient(settings=_settings(mode), transport=httpx.MockTransport(handler))


def test_extended_get_methods_post_expected_service_and_method_without_leaking_token():
    cases = [
        ("bids_get", (710,), "bids", "get"),
        ("changes_check", tuple(), "changes", "check"),
        ("changes_get", tuple(), "changes", "get"),
        ("dictionaries_get", tuple(), "dictionaries", "get"),
        ("bidmodifiers_get", (710,), "bidmodifiers", "get"),
        ("negativekeywords_get", (710, [1]), "negativekeywordsharedsets", "get"),
        ("retargetinglists_get", tuple(), "retargetinglists", "get"),
        ("audiencetargets_get", (710,), "audiencetargets", "get"),
        ("sitelinks_get", tuple(), "sitelinks", "get"),
        ("vcards_get", tuple(), "vcards", "get"),
        ("adimages_get", tuple(), "adimages", "get"),
        ("creatives_get", tuple(), "creatives", "get"),
        ("feeds_get", tuple(), "feeds", "get"),
        ("businesses_get", tuple(), "businesses", "get"),
        ("agencyclients_get", tuple(), "agencyclients", "get"),
    ]

    for method_name, args, service, direct_method in cases:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"result": {"Items": [{"Id": 1}]}})

        client = _client(handler)
        result = getattr(client, method_name)(*args)

        assert captured["url"] == f"https://api.direct.yandex.com/json/v5/{service}"
        assert captured["body"]["method"] == direct_method
        assert result["ok"] is True
        assert "SECRET-TOKEN" not in str(result)


def test_keywordsresearch_methods_post_expected_payloads():
    captured_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"result": {"ok": True}})

    client = _client(handler)

    client.keywordsresearch_has_search_volume(["ремонт посудомоек"])
    client.keywordsresearch_deduplicate(["ремонт", "ремонт"])
    client.keywordsresearch_create_wordstat_report(["ремонт"], [213])
    client.keywordsresearch_get_wordstat_report(123)
    client.keywordsresearch_delete_wordstat_report(123)

    assert [body["method"] for body in captured_bodies] == [
        "hasSearchVolume",
        "deduplicate",
        "createNewWordstatReport",
        "getWordstatReport",
        "deleteWordstatReport",
    ]
    assert captured_bodies[0]["params"] == {"Keywords": ["ремонт посудомоек"]}
    assert captured_bodies[2]["params"] == {"Phrases": ["ремонт"], "GeoID": [213]}
    assert captured_bodies[3]["params"] == {"ReportID": 123}


def test_report_method_posts_to_reports_endpoint_with_report_name():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, text="Date,Clicks\n2026-06-06,1\n")

    client = _client(handler)
    result = client.report("CAMPAIGN_PERFORMANCE_REPORT", date_from="2026-06-01", date_to="2026-06-06")

    assert captured["url"] == "https://api.direct.yandex.com/json/v5/reports"
    assert captured["body"]["params"]["ReportName"] == "directpilot-campaign-performance-report"
    assert captured["body"]["params"]["ReportType"] == "CAMPAIGN_PERFORMANCE_REPORT"
    assert captured["body"]["params"]["DateRangeType"] == "CUSTOM_DATE"
    assert result["ok"] is True
    assert "SECRET-TOKEN" not in str(result)


def test_raw_endpoint_for_extended_get_method_returns_source_yandex_and_read_only():
    from app import main as main_mod

    settings = _settings()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"NegativeKeywords": [{"Id": 1}]}})

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))

    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj
    try:
        response = TestClient(app).get("/yandex/campaigns/710/negative-keywords?ids=1")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert body["service"] == "negativekeywordsharedsets"
    assert captured["body"]["method"] == "get"
    assert "SECRET-TOKEN" not in response.text
