from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.store import store
from app.yandex_direct import YandexDirectClient, YandexDirectError


CAMPAIGN_ID = "713397771"
SOURCE_SET_ID = 700
NEW_SET_ID = 900
SOURCE_ITEMS = [
    {
        "Title": "Problems",
        "Href": "https://old.example.test/stiralnye/#problems",
        "Description": "Known issues",
    },
    {
        "Title": "FAQ",
        "Href": "https://old.example.test/stiralnye/#faq",
        "Description": "Answers",
    },
]
TARGET_ITEMS = [
    {
        "title": "Problems",
        "href": "https://new.example.test/stiralnye/?source=yandex#problems",
        "description": "Known issues",
    },
    {
        "title": "FAQ",
        "href": "https://new.example.test/stiralnye/#faq",
        "description": "Answers",
    },
]


def _settings(mode: str = "live_readonly") -> Settings:
    return Settings(
        _env_file=None,
        directpilot_mode=mode,
        yandex_oauth_token="test-token",
        url_migration_allowed_hosts="new.example.test",
    )


def _ad(
    ad_id: int,
    *,
    href: str | None = None,
    campaign_id: int | str = CAMPAIGN_ID,
    ad_type: str = "TEXT_AD",
    sitelink_set_id: int | None = SOURCE_SET_ID,
) -> dict[str, Any]:
    text_ad: dict[str, Any] = {
        "Title": f"Title {ad_id}",
        "Text": f"Text {ad_id}",
        "Href": href or f"https://old.example.test/ad-{ad_id}?utm_content={ad_id}",
    }
    if sitelink_set_id is not None:
        text_ad["SitelinkSetId"] = sitelink_set_id
    return {
        "Id": ad_id,
        "CampaignId": int(campaign_id),
        "AdGroupId": 5000 + ad_id,
        "Type": ad_type,
        "TextAd": text_ad,
    }


class _MigrationClient:
    """In-memory provider fixture; it never performs network I/O."""

    def __init__(
        self,
        *,
        ads: list[dict[str, Any]] | None = None,
        campaigns: list[dict[str, Any]] | None = None,
        campaigns_response: dict[str, Any] | None = None,
        scan_pages: dict[int, dict[str, Any]] | None = None,
        add_response: dict[str, Any] | None = None,
        update_response: dict[str, Any] | None = None,
        apply_updates: bool = True,
        clone_readback_items: list[dict[str, Any]] | None = None,
    ) -> None:
        source_ads = ads if ads is not None else [_ad(101), _ad(102)]
        self.ads = {int(item["Id"]): copy.deepcopy(item) for item in source_ads}
        if campaigns is None:
            campaign_ids = sorted(
                {
                    int(item["CampaignId"])
                    for item in source_ads
                    if isinstance(item.get("CampaignId"), (int, str))
                    and str(item["CampaignId"]).isdigit()
                }
            )
            campaigns = [{"Id": campaign_id} for campaign_id in campaign_ids]
        self.campaigns = copy.deepcopy(campaigns)
        self.campaigns_response = (
            copy.deepcopy(campaigns_response) if campaigns_response is not None else None
        )
        self.sitelink_sets: dict[int, dict[str, Any]] = {
            SOURCE_SET_ID: {"Id": SOURCE_SET_ID, "Sitelinks": copy.deepcopy(SOURCE_ITEMS)}
        }
        self.scan_pages = copy.deepcopy(scan_pages) if scan_pages is not None else None
        self.add_response = copy.deepcopy(add_response) if add_response is not None else None
        self.update_response = copy.deepcopy(update_response) if update_response is not None else None
        self.apply_updates = apply_updates
        self.clone_readback_items = copy.deepcopy(clone_readback_items)
        self.read_calls: list[tuple[str, list[int]]] = []
        self.scan_calls: list[tuple[list[int], int, int]] = []
        self.campaigns_get_calls = 0
        self.add_calls: list[list[dict[str, Any]]] = []
        self.update_calls: list[list[dict[str, Any]]] = []
        self.sitelinks_update_calls = 0

    def campaigns_get(self) -> dict[str, Any]:
        self.campaigns_get_calls += 1
        if self.campaigns_response is not None:
            return copy.deepcopy(self.campaigns_response)
        return {"ok": True, "result": {"Campaigns": copy.deepcopy(self.campaigns)}}

    def ads_get_by_campaign_and_ids(self, campaign_id: str, ad_ids: list[int]) -> dict[str, Any]:
        self.read_calls.append((str(campaign_id), list(ad_ids)))
        return {
            "ok": True,
            "result": {"Ads": [copy.deepcopy(self.ads[ad_id]) for ad_id in ad_ids if ad_id in self.ads]},
        }

    def ads_get_by_campaign_ids(
        self,
        campaign_ids: list[int],
        *,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        self.scan_calls.append((list(campaign_ids), limit, offset))
        if self.scan_pages is not None:
            return {"ok": True, "result": copy.deepcopy(self.scan_pages[offset])}
        selected_campaign_ids = {str(item) for item in campaign_ids}
        rows = [
            copy.deepcopy(ad)
            for ad in self.ads.values()
            if str(ad.get("CampaignId")) in selected_campaign_ids
        ]
        return {"ok": True, "result": {"Ads": rows}}

    def sitelinks_get(self, ids: list[int]) -> dict[str, Any]:
        return {
            "ok": True,
            "result": {
                "SitelinksSets": [
                    copy.deepcopy(self.sitelink_sets[item])
                    for item in ids
                    if item in self.sitelink_sets
                ]
            },
        }

    def sitelinks_add(self, sitelink_sets: list[dict[str, Any]]) -> dict[str, Any]:
        self.add_calls.append(copy.deepcopy(sitelink_sets))
        response = copy.deepcopy(self.add_response)
        if response is None:
            response = {"ok": True, "result": {"AddResults": [{"Id": NEW_SET_ID}]}}
        results = ((response.get("result") or {}).get("AddResults") or []) if response.get("ok") else []
        if results and isinstance(results[0], dict) and not results[0].get("Errors"):
            created_id = int(results[0]["Id"])
            created = copy.deepcopy(sitelink_sets[0])
            created["Id"] = created_id
            if self.clone_readback_items is not None:
                created["Sitelinks"] = copy.deepcopy(self.clone_readback_items)
            self.sitelink_sets[created_id] = created
        return response

    def ads_update(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        self.update_calls.append(copy.deepcopy(items))
        response = copy.deepcopy(self.update_response)
        if response is None:
            response = {
                "ok": True,
                "result": {"UpdateResults": [{"Id": item["Id"]} for item in items]},
            }
        results = ((response.get("result") or {}).get("UpdateResults") or []) if response.get("ok") else []
        if self.apply_updates:
            for item, result in zip(items, results, strict=False):
                if not isinstance(result, dict) or result.get("Errors"):
                    continue
                if str(result.get("Id")) != str(item["Id"]):
                    continue
                self.ads[int(item["Id"])]["TextAd"].update(copy.deepcopy(item["TextAd"]))
        return response

    def sitelinks_update(self, _items: list[dict[str, Any]]) -> dict[str, Any]:
        self.sitelinks_update_calls += 1
        raise AssertionError("URL migration must never call undocumented sitelinks.update")


def _install_public_url_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        store,
        "_url_migration_dns_resolver",
        lambda _host, _port: ["93.184.216.34"],
        raising=False,
    )
    monkeypatch.setattr(
        store,
        "_url_migration_http_fetcher",
        lambda _url: httpx.Response(200, text="<main id='problems'></main><section id='faq'></section>"),
        raising=False,
    )
    monkeypatch.setattr(store, "_url_migration_results_by_key", {}, raising=False)


def _post_migration(
    path: str,
    payload: dict[str, Any],
    *,
    client: _MigrationClient,
    settings: Settings,
) -> httpx.Response:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: client
    try:
        return TestClient(app).post(path, json=payload)
    finally:
        app.dependency_overrides.clear()


def _sitelink_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_sitelink_set_id": SOURCE_SET_ID,
        "expected_items": [
            {"title": item["Title"], "href": item["Href"], "description": item["Description"]}
            for item in SOURCE_ITEMS
        ],
        "target_items": copy.deepcopy(TARGET_ITEMS),
    }
    body.update(overrides)
    return body


def _landing_body(
    *,
    ad_id: int = 101,
    expected_href: str | None = None,
    target_href: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "items": [
            {
                "ad_id": ad_id,
                "expected_href": expected_href or f"https://old.example.test/ad-{ad_id}?utm_content={ad_id}",
                "target_href": target_href or f"https://new.example.test/ad-{ad_id}?utm_content={ad_id}",
            }
        ]
    }
    body.update(overrides)
    return body


def test_client_exposes_documented_sitelinks_add_method() -> None:
    assert callable(getattr(YandexDirectClient, "sitelinks_add", None))


def test_sitelinks_add_posts_documented_payload_and_returns_add_results() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 91}]}})

    client = YandexDirectClient(_settings("live_write"), transport=httpx.MockTransport(handler))
    result = client.sitelinks_add(
        [
            {
                "Sitelinks": [
                    {
                        "Title": "Prices",
                        "Href": "https://example.test/prices",
                        "Description": "Price list",
                    }
                ]
            }
        ]
    )

    assert captured["url"] == "https://api.direct.yandex.com/json/v5/sitelinks"
    assert captured["body"] == {
        "method": "add",
        "params": {
            "SitelinksSets": [
                {
                    "Sitelinks": [
                        {
                            "Title": "Prices",
                            "Href": "https://example.test/prices",
                            "Description": "Price list",
                        }
                    ]
                }
            ]
        },
    }
    assert result["result"]["AddResults"] == [{"Id": 91}]


def test_client_builds_campaign_scoped_and_account_reference_reads() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"result": {"Ads": []}})

    client = YandexDirectClient(_settings(), transport=httpx.MockTransport(handler))
    client.ads_get_by_campaign_and_ids(CAMPAIGN_ID, [101])
    client.ads_get_by_campaign_ids([int(CAMPAIGN_ID), 999], limit=200, offset=20)

    assert captured[0]["params"]["SelectionCriteria"] == {
        "CampaignIds": [int(CAMPAIGN_ID)],
        "Ids": [101],
        "Types": ["TEXT_AD"],
    }
    assert captured[1]["params"]["SelectionCriteria"] == {
        "CampaignIds": [int(CAMPAIGN_ID), 999]
    }
    assert "SitelinkSetIds" not in captured[1]["params"]["SelectionCriteria"]
    assert captured[1]["params"]["FieldNames"] == ["Id", "CampaignId", "AdGroupId", "Type"]
    assert captured[1]["params"]["TextAdFieldNames"] == ["Href", "SitelinkSetId"]
    assert captured[1]["params"]["Page"] == {"Limit": 200, "Offset": 20}


def test_client_http_error_diagnostics_are_allowlisted() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "error_code": "BAD_REQUEST",
                    "error_string": "Structured failure",
                    "error_detail": "safe diagnostic detail",
                    "token": "should-not-leak",
                    "raw_body": "should-not-leak",
                }
            },
            headers={"X-Internal": "should-not-leak"},
        )

    client = YandexDirectClient(_settings(), transport=httpx.MockTransport(handler))

    with pytest.raises(YandexDirectError) as raised:
        client.sitelinks_add([])

    diagnostics = raised.value.diagnostics
    assert diagnostics == {
        "provider": "yandex_direct",
        "service": "sitelinks",
        "method": "add",
        "http_status": 400,
        "error_code": "BAD_REQUEST",
        "error_string": "Structured failure",
        "error_detail": "safe diagnostic detail",
    }
    assert "should-not-leak" not in json.dumps(diagnostics)
    assert "raw_body" not in diagnostics
    assert "token" not in diagnostics


def test_sitelink_reference_scan_error_is_redacted_in_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _MigrationClient()
    _install_public_url_fakes(monkeypatch)

    def rejected_scan(
        campaign_ids: list[int], *, limit: int, offset: int
    ) -> dict[str, Any]:
        assert campaign_ids == [int(CAMPAIGN_ID)]
        assert limit == 10_000
        assert offset == 0
        return {
            "ok": False,
            "error": {
                "error_code": "SCAN_REJECTED",
                "error_string": "Reference scan rejected",
                "error_detail": "safe diagnostic detail",
                "token": "should-not-leak",
                "raw_body": "should-not-leak",
            },
            "units": "17/1000",
            "request": "should-not-leak",
        }

    monkeypatch.setattr(fake, "ads_get_by_campaign_ids", rejected_scan)
    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/sitelinks/migrate-urls",
        _sitelink_body(),
        client=fake,
        settings=_settings(),
    )

    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert detail["operation"] == "sitelink_reference_scan"
    assert detail["service"] == "ads"
    assert detail["method"] == "get"
    assert detail["error_code"] == "SCAN_REJECTED"
    assert detail["error_string"] == "Reference scan rejected"
    assert detail["error_detail"] == "safe diagnostic detail"
    assert "should-not-leak" not in json.dumps(detail)
    assert "token" not in detail
    assert "raw_body" not in detail
    assert "request" not in detail
    assert "units" not in detail


def test_sitelink_campaign_inventory_error_is_redacted_in_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _MigrationClient(
        campaigns_response={
            "ok": False,
            "error": {
                "error_code": "CAMPAIGNS_REJECTED",
                "error_string": "Campaign inventory rejected",
                "error_detail": "safe diagnostic detail",
                "token": "should-not-leak",
            },
            "units": "17/1000",
        }
    )
    _install_public_url_fakes(monkeypatch)

    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/sitelinks/migrate-urls",
        _sitelink_body(),
        client=fake,
        settings=_settings(),
    )

    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert detail["operation"] == "sitelink_reference_campaign_inventory"
    assert detail["service"] == "campaigns"
    assert detail["method"] == "get"
    assert detail["error_code"] == "CAMPAIGNS_REJECTED"
    assert detail["error_string"] == "Campaign inventory rejected"
    assert detail["error_detail"] == "safe diagnostic detail"
    assert "should-not-leak" not in json.dumps(detail)
    assert "units" not in detail
    assert fake.scan_calls == []


def test_sitelink_preview_is_clone_only_and_uses_minimal_attach_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _MigrationClient()
    _install_public_url_fakes(monkeypatch)

    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/sitelinks/migrate-urls",
        _sitelink_body(),
        client=fake,
        settings=_settings(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["completed"] is False
    assert body["reference_scan"]["complete"] is True
    assert body["reference_scan"]["route_reference_ad_ids"] == [101, 102]
    assert body["payload_preview"]["sitelinks.add"] == {
        "method": "add",
        "params": {"SitelinksSets": [{"Sitelinks": [
            {
                "Title": "Problems",
                "Href": "https://new.example.test/stiralnye/?source=yandex#problems",
                "Description": "Known issues",
            },
            {
                "Title": "FAQ",
                "Href": "https://new.example.test/stiralnye/#faq",
                "Description": "Answers",
            },
        ]}]},
    }
    assert body["payload_preview"]["ads.update"]["params"]["Ads"] == [
        {
            "Id": 101,
            "TextAd": {
                "Href": "https://old.example.test/ad-101?utm_content=101",
                "SitelinkSetId": "$new_sitelink_set_id",
            },
        },
        {
            "Id": 102,
            "TextAd": {
                "Href": "https://old.example.test/ad-102?utm_content=102",
                "SitelinkSetId": "$new_sitelink_set_id",
            },
        },
    ]
    assert fake.add_calls == []
    assert fake.update_calls == []
    assert fake.sitelinks_update_calls == 0


def test_sitelink_apply_clones_reads_back_and_reattaches_only_route_text_ads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _MigrationClient(
        ads=[_ad(101), _ad(102), _ad(303, campaign_id=999)],
        add_response={
            "ok": True,
            "result": {
                "AddResults": [
                    {"Id": NEW_SET_ID, "Warnings": [{"Code": 7, "Message": "do not expose"}]}
                ]
            },
        },
    )
    _install_public_url_fakes(monkeypatch)
    body = _sitelink_body(dry_run=False, approved=True, idempotency_key="sitelink-apply-001")

    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/sitelinks/migrate-urls",
        body,
        client=fake,
        settings=_settings("live_write"),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["completed"] is True
    assert result["applied"] is True
    assert result["partial_failure"] is False
    assert result["new_sitelink_set_id"] == NEW_SET_ID
    assert result["provider_results"]["sitelinks.add"][0]["warnings"] == [
        {"code": 7, "message": "Provider warning"}
    ]
    assert "do not expose" not in response.text
    assert fake.add_calls == [[{"Sitelinks": [
        {
            "Title": "Problems",
            "Href": "https://new.example.test/stiralnye/?source=yandex#problems",
            "Description": "Known issues",
        },
        {
            "Title": "FAQ",
            "Href": "https://new.example.test/stiralnye/#faq",
            "Description": "Answers",
        },
    ]}]]
    assert fake.update_calls == [[
        {"Id": 101, "TextAd": {"Href": "https://old.example.test/ad-101?utm_content=101", "SitelinkSetId": NEW_SET_ID}},
        {"Id": 102, "TextAd": {"Href": "https://old.example.test/ad-102?utm_content=102", "SitelinkSetId": NEW_SET_ID}},
    ]]
    assert fake.ads[101]["TextAd"]["SitelinkSetId"] == NEW_SET_ID
    assert fake.ads[303]["TextAd"]["SitelinkSetId"] == SOURCE_SET_ID
    assert fake.sitelink_sets[SOURCE_SET_ID]["Sitelinks"] == SOURCE_ITEMS
    assert fake.sitelinks_update_calls == 0


def test_unified_preview_and_apply_combines_target_href_with_new_sitelink_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview_fake = _MigrationClient()
    _install_public_url_fakes(monkeypatch)
    body = {
        "ad_items": [
            _landing_body(ad_id=101)["items"][0],
            _landing_body(ad_id=102)["items"][0],
        ],
        "sitelink_migration": _sitelink_body(),
    }
    preview = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/landing-url-migrations",
        body,
        client=preview_fake,
        settings=_settings(),
    )
    assert preview.status_code == 200, preview.text
    preview_ads = preview.json()["payload_preview"]["ads.update"]["params"]["Ads"]
    assert preview_ads == [
        {
            "Id": 101,
            "TextAd": {
                "Href": "https://new.example.test/ad-101?utm_content=101",
                "SitelinkSetId": "$new_sitelink_set_id",
            },
        },
        {
            "Id": 102,
            "TextAd": {
                "Href": "https://new.example.test/ad-102?utm_content=102",
                "SitelinkSetId": "$new_sitelink_set_id",
            },
        },
    ]
    assert preview_fake.add_calls == []
    assert preview_fake.update_calls == []

    apply_fake = _MigrationClient()
    apply_body = copy.deepcopy(body)
    apply_body.update(dry_run=False, approved=True, idempotency_key="unified-apply-001")
    applied = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/landing-url-migrations",
        apply_body,
        client=apply_fake,
        settings=_settings("live_write"),
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["completed"] is True
    assert apply_fake.update_calls == [[
        {"Id": 101, "TextAd": {"Href": "https://new.example.test/ad-101?utm_content=101", "SitelinkSetId": NEW_SET_ID}},
        {"Id": 102, "TextAd": {"Href": "https://new.example.test/ad-102?utm_content=102", "SitelinkSetId": NEW_SET_ID}},
    ]]


@pytest.mark.parametrize(
    ("ads", "expected_status"),
    [
        ([_ad(101, campaign_id=999)], 502),
        ([_ad(101, ad_type="IMAGE_AD")], 502),
        ([_ad(101, href="https://old.example.test/unexpected")], 409),
    ],
    ids=["ownership", "type", "expected-href"],
)
def test_landing_preflight_fails_closed_on_ownership_type_and_expected_href(
    monkeypatch: pytest.MonkeyPatch,
    ads: list[dict[str, Any]],
    expected_status: int,
) -> None:
    fake = _MigrationClient(ads=ads)
    _install_public_url_fakes(monkeypatch)

    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/ads/landing-urls",
        _landing_body(),
        client=fake,
        settings=_settings(),
    )

    assert response.status_code == expected_status
    assert fake.update_calls == []


def test_sitelink_preflight_requires_exact_source_title_href_and_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _MigrationClient()
    fake.sitelink_sets[SOURCE_SET_ID]["Sitelinks"][0]["Description"] = "changed"
    _install_public_url_fakes(monkeypatch)

    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/sitelinks/migrate-urls",
        _sitelink_body(),
        client=fake,
        settings=_settings(),
    )

    assert response.status_code == 409
    assert fake.add_calls == []
    assert fake.update_calls == []


def test_sitelink_add_per_item_error_stops_before_ads_update(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _MigrationClient(
        add_response={
            "ok": True,
            "result": {"AddResults": [{"Errors": [{"Code": 42, "Message": "do not expose"}]}]},
        }
    )
    _install_public_url_fakes(monkeypatch)

    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/sitelinks/migrate-urls",
        _sitelink_body(dry_run=False, approved=True, idempotency_key="sitelink-error-001"),
        client=fake,
        settings=_settings("live_write"),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["completed"] is False
    assert result["applied"] is False
    assert result["partial_failure"] is True
    assert result["provider_results"]["sitelinks.add"][0]["errors"] == [
        {"code": 42, "message": "Provider error"}
    ]
    assert "do not expose" not in response.text
    assert fake.update_calls == []


def test_unexpected_sitelinks_add_result_stops_before_ads_update(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _MigrationClient(
        add_response={
            "ok": True,
            "result": {"AddResults": [{"Id": NEW_SET_ID}, {"Id": NEW_SET_ID + 1}]},
        }
    )
    _install_public_url_fakes(monkeypatch)

    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/sitelinks/migrate-urls",
        _sitelink_body(dry_run=False, approved=True, idempotency_key="sitelink-extra-001"),
        client=fake,
        settings=_settings("live_write"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["completed"] is False
    assert fake.update_calls == []


def test_non_positive_sitelinks_add_id_stops_before_ads_update(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _MigrationClient(add_response={"ok": True, "result": {"AddResults": [{"Id": 0}]}})
    _install_public_url_fakes(monkeypatch)

    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/sitelinks/migrate-urls",
        _sitelink_body(dry_run=False, approved=True, idempotency_key="sitelink-zero-id-001"),
        client=fake,
        settings=_settings("live_write"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["completed"] is False
    assert response.json()["partial_failure"] is True
    assert fake.update_calls == []


def test_clone_readback_mismatch_never_reattaches_ads(monkeypatch: pytest.MonkeyPatch) -> None:
    mismatched_clone = [
        {
            "Title": "Problems",
            "Href": "https://new.example.test/wrong#problems",
            "Description": "Known issues",
        },
        {
            "Title": "FAQ",
            "Href": "https://new.example.test/stiralnye/#faq",
            "Description": "Answers",
        },
    ]
    fake = _MigrationClient(clone_readback_items=mismatched_clone)
    _install_public_url_fakes(monkeypatch)

    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/sitelinks/migrate-urls",
        _sitelink_body(dry_run=False, approved=True, idempotency_key="sitelink-readback-001"),
        client=fake,
        settings=_settings("live_write"),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["completed"] is False
    assert result["applied"] is False
    assert result["partial_failure"] is True
    assert result["stage"] == "sitelinks.readback"
    assert result["recovery_note"]
    assert fake.update_calls == []


@pytest.mark.parametrize(
    ("update_response", "apply_updates"),
    [
        (
            {
                "ok": True,
                "result": {
                    "UpdateResults": [
                        {"Id": 101},
                        {"Id": 102, "Errors": [{"Code": 55, "Message": "redacted"}]},
                    ]
                },
            },
            True,
        ),
        (
            {"ok": True, "result": {"UpdateResults": [{"Id": 101}, {"Id": 102}]}},
            False,
        ),
    ],
    ids=["partial-update-results", "readback-mismatch"],
)
def test_unified_apply_never_completes_after_provider_partial_or_readback_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    update_response: dict[str, Any],
    apply_updates: bool,
) -> None:
    fake = _MigrationClient(update_response=update_response, apply_updates=apply_updates)
    _install_public_url_fakes(monkeypatch)
    body = {
        "ad_items": [
            _landing_body(ad_id=101)["items"][0],
            _landing_body(ad_id=102)["items"][0],
        ],
        "sitelink_migration": _sitelink_body(),
        "dry_run": False,
        "approved": True,
        "idempotency_key": f"unified-partial-{int(apply_updates)}-001",
    }

    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/landing-url-migrations",
        body,
        client=fake,
        settings=_settings("live_write"),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["completed"] is False
    assert result["applied"] is False
    assert result["partial_failure"] is True
    assert result["recovery_note"]


def test_landing_apply_idempotency_replays_exact_result_and_rejects_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _MigrationClient()
    _install_public_url_fakes(monkeypatch)
    body = _landing_body(dry_run=False, approved=True, idempotency_key="landing-idempotency-001")
    path = f"/yandex/campaigns/{CAMPAIGN_ID}/ads/landing-urls"

    first = _post_migration(path, body, client=fake, settings=_settings("live_write"))
    second = _post_migration(path, body, client=fake, settings=_settings("live_write"))
    collision_body = _landing_body(
        dry_run=False,
        approved=True,
        idempotency_key="landing-idempotency-001",
        target_href="https://new.example.test/changed",
    )
    collision = _post_migration(path, collision_body, client=fake, settings=_settings("live_write"))
    cross_namespace_fake = _MigrationClient()
    cross_namespace = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/sitelinks/migrate-urls",
        _sitelink_body(
            dry_run=False,
            approved=True,
            idempotency_key="landing-idempotency-001",
        ),
        client=cross_namespace_fake,
        settings=_settings("live_write"),
    )

    assert first.status_code == 200, first.text
    assert first.json()["completed"] is True
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert collision.status_code == 409
    assert cross_namespace.status_code == 409
    assert cross_namespace_fake.add_calls == []
    assert cross_namespace_fake.update_calls == []
    assert len(fake.update_calls) == 1


def test_sitelink_reference_scan_paginates_filters_unrelated_rows_and_fails_closed() -> None:
    first = _ad(101)
    second = _ad(102)
    unrelated = _ad(303, sitelink_set_id=SOURCE_SET_ID + 1)
    without_sitelink = _ad(404, sitelink_set_id=None)
    paged = _MigrationClient(
        ads=[first, second, unrelated, without_sitelink],
        campaigns=[{"Id": int(CAMPAIGN_ID)}, {"Id": 999}],
        scan_pages={
            0: {"Ads": [first, unrelated], "LimitedBy": 2},
            2: {"Ads": [without_sitelink, second]},
        },
    )
    refs = store._url_migration_scan_sitelink_references(paged, SOURCE_SET_ID)
    assert [item["ad_id"] for item in refs] == [101, 102]
    assert paged.campaigns_get_calls == 1
    assert paged.scan_calls == [
        ([int(CAMPAIGN_ID), 999], 10_000, 0),
        ([int(CAMPAIGN_ID), 999], 10_000, 2),
    ]

    repeated_cursor = _MigrationClient(
        ads=[first, second],
        scan_pages={0: {"Ads": [first], "LimitedBy": 1}, 1: {"Ads": [second], "LimitedBy": 1}},
    )
    duplicate = _MigrationClient(
        ads=[first],
        scan_pages={0: {"Ads": [first], "LimitedBy": 1}, 1: {"Ads": [first]}},
    )
    truncated_without_progress = _MigrationClient(
        ads=[first],
        scan_pages={0: {"Ads": [], "LimitedBy": 1}},
    )
    rejected_inventories = (
        _MigrationClient(campaigns_response={"ok": False, "error": {}}),
        _MigrationClient(campaigns_response={"ok": True, "result": {"Campaigns": [], "LimitedBy": 1}}),
        _MigrationClient(campaigns_response={"ok": True, "result": {"Campaigns": "malformed"}}),
        _MigrationClient(campaigns=[]),
        _MigrationClient(campaigns=[{"Id": "not-a-number"}]),
        _MigrationClient(campaigns=[{"Id": 1}, {"Id": "1"}]),
    )
    for client in (
        repeated_cursor,
        duplicate,
        truncated_without_progress,
        *rejected_inventories,
    ):
        with pytest.raises(YandexDirectError):
            store._url_migration_scan_sitelink_references(client, SOURCE_SET_ID)


def test_sitelink_target_fetch_timeout_returns_safe_409(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _MigrationClient()
    _install_public_url_fakes(monkeypatch)

    def timeout(_url: str) -> httpx.Response:
        raise httpx.ReadTimeout("should-not-leak")

    monkeypatch.setattr(store, "_url_migration_http_fetcher", timeout, raising=False)
    response = _post_migration(
        f"/yandex/campaigns/{CAMPAIGN_ID}/sitelinks/migrate-urls",
        _sitelink_body(),
        client=fake,
        settings=_settings(),
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Target URL could not be fetched safely"
    assert "should-not-leak" not in response.text
