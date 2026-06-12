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


def test_keywordsresearch_has_search_volume_posts_v5_selection_criteria_with_default_region():
    captured_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"result": {"Keywords": []}})

    client = _client(handler)
    result = client.keywordsresearch_has_search_volume(["ремонт посудомоек"])

    assert captured_bodies[0]["method"] == "hasSearchVolume"
    params = captured_bodies[0]["params"]
    assert params["SelectionCriteria"]["Keywords"] == ["ремонт посудомоек"]
    # 43 is the default region (Moscow) when caller does not pass region_ids.
    assert params["SelectionCriteria"]["RegionIds"] == [43]
    assert params["FieldNames"] == [
        "Keyword",
        "RegionIds",
        "AllDevices",
        "MobilePhones",
        "Tablets",
        "Desktops",
    ]
    assert result["ok"] is True


def test_keywordsresearch_has_search_volume_accepts_custom_regions_and_field_names():
    captured_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"result": {"Keywords": []}})

    client = _client(handler)
    client.keywordsresearch_has_search_volume(
        ["ремонт"],
        region_ids=[213, 1],
        field_names=["Keyword", "AllDevices"],
    )

    params = captured_bodies[0]["params"]
    assert params["SelectionCriteria"]["RegionIds"] == [213, 1]
    assert params["FieldNames"] == ["Keyword", "AllDevices"]


def test_keywordsresearch_deduplicate_posts_v5_payload_with_keyword_objects():
    captured_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"result": {"Keywords": []}})

    client = _client(handler)
    result = client.keywordsresearch_deduplicate(
        ["ремонт", "ремонт посудомоек"],
    )

    assert captured_bodies[0]["method"] == "deduplicate"
    params = captured_bodies[0]["params"]
    # Direct v5 expects Keywords as a list of objects, not bare strings.
    assert params["Keywords"] == [
        {"Keyword": "ремонт"},
        {"Keyword": "ремонт посудомоек"},
    ]
    assert result["ok"] is True


def test_keywordsresearch_deduplicate_preserves_optional_id_and_weight():
    captured_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"result": {"Keywords": []}})

    client = _client(handler)
    client.keywordsresearch_deduplicate(
        [
            {"Keyword": "ремонт", "Id": 123, "Weight": 5},
            "ремонт посудомоек",
        ]
    )

    params = captured_bodies[0]["params"]
    assert params["Keywords"] == [
        {"Keyword": "ремонт", "Id": 123, "Weight": 5},
        {"Keyword": "ремонт посудомоек"},
    ]


def test_keywordsresearch_wordstat_methods_do_not_call_v5_and_return_unsupported_envelope():
    """Wordstat create/get/delete are not part of the v5 keywordsresearch service.

    The current v5 client must not pretend to call them; instead it returns a
    safe {ok: False, error_code: ...} envelope and never touches the transport.
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"result": {}})

    client = _client(handler)

    create_result = client.keywordsresearch_create_wordstat_report(["ремонт"], [213])
    get_result = client.keywordsresearch_get_wordstat_report(123)
    delete_result = client.keywordsresearch_delete_wordstat_report(123)

    # Transport must not be hit: there is no v5 method for these operations.
    assert calls == []
    for envelope in (create_result, get_result, delete_result):
        assert envelope["ok"] is False
        assert envelope["error"]["error_code"] == "UNSUPPORTED_IN_V5"
        # Make sure the v5 endpoint path is not in the error envelope: the
        # client returned without any network call.
        assert "/json/v5/keywordsresearch" not in envelope["error"].get(
            "error_detail", ""
        )
    # The same token-leakage guard we use for live calls must still hold.
    assert "SECRET-TOKEN" not in str(create_result)
    assert "SECRET-TOKEN" not in str(get_result)
    assert "SECRET-TOKEN" not in str(delete_result)


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
    assert captured["body"]["params"]["ReportName"].startswith(
        "directpilot-campaign-performance-report-"
    )
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


def test_wordstat_endpoints_surface_unsupported_in_v5_error_and_skip_network():
    """The wordstat endpoints must short-circuit with a clear unsupported
    error and never invoke the v5 transport — even when a real Yandex
    DirectClient is wired into the FastAPI app."""
    from app import main as main_mod

    settings = _settings()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"result": {}})

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))

    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj
    try:
        api = TestClient(app)
        create_resp = api.get(
            "/yandex/keywords-research/wordstat/create?phrases=%D1%80%D0%B5%D0%BC%D0%BE%D0%BD%D1%82&geo_ids=213"
        )
        get_resp = api.get("/yandex/keywords-research/wordstat/123")
        delete_resp = api.delete("/yandex/keywords-research/wordstat/123")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert calls == []
    for resp in (create_resp, get_resp, delete_resp):
        assert resp.status_code == 502, resp.text
        detail = resp.json()["detail"]
        assert detail["error_type"] == "YandexDirectError"
        assert detail["message"].startswith("Yandex Direct rejected keywordsresearch.")
        assert "UNSUPPORTED_IN_V5" in detail["message"]
        assert "SECRET-TOKEN" not in resp.text
