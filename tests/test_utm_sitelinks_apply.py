"""Focused tests for UTM sitelink plan/apply behavior."""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.yandex_direct import YandexDirectClient


def _settings(mode: str = "live_readonly") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token="placeholder-token")


def _make_client(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


def _ad_with_sitelink_set(href: str = "https://example.ru/main") -> dict[str, Any]:
    return {
        "Id": 101,
        "AdGroupId": 10,
        "CampaignId": 12345,
        "Status": "ACCEPTED",
        "State": "ON",
        "Type": "TEXT_AD",
        "TextAd": {
            "Title": "Test Ad",
            "Text": "Test text",
            "Href": href,
            "SitelinkSetId": 5001,
        },
    }


def _sitelink_set(*, prices_href: str, reviews_href: str | None = None) -> dict[str, Any]:
    links = [
        {"Title": "Цены", "Href": prices_href, "Description": "Прайс"},
    ]
    if reviews_href is not None:
        links.append({"Title": "Отзывы", "Href": reviews_href, "Description": "Отзывы клиентов"})
    return {"Id": 5001, "Sitelinks": links}


class TestUtmSitelinksPlan:
    def test_plan_sitelinks_preserves_fragment_and_existing_query(self):
        settings = Settings(_env_file=None, directpilot_mode="mock")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-plan",
                json={"campaign_slug": "remont-pmm-kazan", "include_sitelinks": True},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200, resp.text
        body = resp.json()
        sitelinks = body["sitelink_items"]
        assert sitelinks
        price = next(item for item in sitelinks if item["entity_id"].endswith("/Цены"))
        reviews = next(item for item in sitelinks if item["entity_id"].endswith("/Отзывы"))
        assert price["new_url"].endswith("#price-list")
        assert "utm_campaign=remont-pmm-kazan" in price["new_url"]
        assert "sort=new" in reviews["new_url"]
        assert "utm_source=yandex" in reviews["new_url"]

    def test_plan_preserves_cyrillic_domain_and_fragment(self):
        settings = _settings("live_readonly")

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            if request.url.path.endswith("/ads"):
                return httpx.Response(200, json={"result": {"Ads": [_ad_with_sitelink_set()]}})
            if request.url.path.endswith("/sitelinks"):
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "SitelinksSets": [
                                _sitelink_set(prices_href="https://ремонт-в-казани.рф/#prices")
                            ]
                        }
                    },
                )
            raise AssertionError(f"unexpected request {request.url} {body}")

        yandex = _make_client(settings, handler)
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            resp = client.post(
                "/yandex/campaigns/12345/utm-plan",
                json={"campaign_slug": "remont-pmm-kazan", "include_sitelinks": True},
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200, resp.text
        new_url = resp.json()["sitelink_items"][0]["new_url"]
        assert new_url == (
            "https://ремонт-в-казани.рф/?utm_source=yandex&utm_medium=cpc"
            "&utm_campaign=remont-pmm-kazan#prices"
        )

    def test_plan_overwrite_false_skips_complete_sitelink_utm(self):
        settings = _settings("live_readonly")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ads"):
                return httpx.Response(200, json={"result": {"Ads": [_ad_with_sitelink_set()]}})
            return httpx.Response(
                200,
                json={
                    "result": {
                        "SitelinksSets": [
                            _sitelink_set(
                                prices_href=(
                                    "https://example.ru/prices?utm_source=yandex"
                                    "&utm_medium=cpc&utm_campaign=old#prices"
                                )
                            )
                        ]
                    }
                },
            )

        yandex = _make_client(settings, handler)
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            no_overwrite = client.post(
                "/yandex/campaigns/12345/utm-plan",
                json={"campaign_slug": "new", "include_sitelinks": True, "overwrite": False},
            )
            overwrite = client.post(
                "/yandex/campaigns/12345/utm-plan",
                json={"campaign_slug": "new", "include_sitelinks": True, "overwrite": True},
            )
        finally:
            app.dependency_overrides.clear()

        assert no_overwrite.status_code == 200, no_overwrite.text
        assert no_overwrite.json()["sitelink_items"][0]["new_url"] == (
            "https://example.ru/prices?utm_source=yandex&utm_medium=cpc&utm_campaign=old#prices"
        )
        assert overwrite.status_code == 200, overwrite.text
        assert overwrite.json()["sitelink_items"][0]["new_url"] == (
            "https://example.ru/prices?utm_source=yandex&utm_medium=cpc&utm_campaign=new#prices"
        )


class TestUtmSitelinksApply:
    def test_apply_dry_run_shows_sitelinks_update_payload_but_does_not_write(self):
        settings = _settings("live_write")
        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            calls.append((request.url.path, body.get("method")))
            if body.get("method") == "update":
                raise AssertionError("dry_run must not call update")
            if request.url.path.endswith("/ads"):
                return httpx.Response(200, json={"result": {"Ads": [_ad_with_sitelink_set()]}})
            if request.url.path.endswith("/sitelinks"):
                return httpx.Response(
                    200,
                    json={"result": {"SitelinksSets": [_sitelink_set(prices_href="https://example.ru/#prices")]}},
                )
            raise AssertionError(f"unexpected request {request.url}")

        yandex = _make_client(settings, handler)
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            resp = client.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-sl-dry-001",
                    "dry_run": True,
                    "campaign_slug": "dry-slug",
                    "include_sitelinks": True,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["applied"] is False
        assert body["sitelink_items"]
        assert body["not_implemented"] == []
        assert body["payload_preview"]["sitelinks_preview"]["params"]["SitelinksSets"][0]["Id"] == 5001
        assert all(method == "get" for _, method in calls)

    def test_apply_include_sitelinks_false_does_not_read_or_update_sitelinks(self):
        settings = _settings("live_write")
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            paths.append(request.url.path)
            assert not request.url.path.endswith("/sitelinks")
            if body.get("method") == "update":
                return httpx.Response(200, json={"result": {}})
            return httpx.Response(200, json={"result": {"Ads": [_ad_with_sitelink_set()]}})

        yandex = _make_client(settings, handler)
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            resp = client.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-no-sl-001",
                    "dry_run": False,
                    "campaign_slug": "no-slug",
                    "include_sitelinks": False,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200, resp.text
        assert resp.json()["sitelink_items"] == []
        assert not any(path.endswith("/sitelinks") for path in paths)

    def test_apply_live_readonly_blocks_before_sitelinks_write(self):
        settings = _settings("live_readonly")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("live_readonly real apply must not call Yandex")

        yandex = _make_client(settings, handler)
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            resp = client.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-ro-sl-001",
                    "dry_run": False,
                    "campaign_slug": "ro-slug",
                    "include_sitelinks": True,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 409

    def test_apply_requires_approval_for_sitelinks(self):
        settings = _settings("live_write")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": False,
                    "idempotency_key": "utm-appr-sl-001",
                    "dry_run": False,
                    "campaign_slug": "approval-slug",
                    "include_sitelinks": True,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 409

    def test_apply_requires_idempotency_key_for_sitelinks(self):
        settings = _settings("live_write")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "bad",
                    "dry_run": False,
                    "campaign_slug": "idemp-slug",
                    "include_sitelinks": True,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422

    def test_successful_apply_updates_sitelinks_and_returns_readback(self):
        settings = _settings("live_write")
        state: dict[str, Any] = {"sitelinks_updated": False, "sitelinks_update_payload": None}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            service = request.url.path.rsplit("/", 1)[-1]
            method = body.get("method")
            if service == "ads" and method == "get":
                return httpx.Response(200, json={"result": {"Ads": [_ad_with_sitelink_set()]}})
            if service == "ads" and method == "update":
                return httpx.Response(200, json={"result": {}})
            if service == "sitelinks" and method == "get":
                href = (
                    "https://example.ru/?utm_source=yandex&utm_medium=cpc&utm_campaign=apply-slug#prices"
                    if state["sitelinks_updated"]
                    else "https://example.ru/#prices"
                )
                return httpx.Response(200, json={"result": {"SitelinksSets": [_sitelink_set(prices_href=href)]}})
            if service == "sitelinks" and method == "update":
                state["sitelinks_updated"] = True
                state["sitelinks_update_payload"] = body
                return httpx.Response(200, json={"result": {}})
            raise AssertionError(f"unexpected request {request.url} {body}")

        yandex = _make_client(settings, handler)
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            resp = client.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-sl-apply-001",
                    "dry_run": False,
                    "campaign_slug": "apply-slug",
                    "include_sitelinks": True,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["applied"] is True
        assert body["not_implemented"] == []
        sent_links = state["sitelinks_update_payload"]["params"]["SitelinksSets"][0]["Sitelinks"]
        assert sent_links[0]["Title"] == "Цены"
        assert sent_links[0]["Description"] == "Прайс"
        assert body["sitelink_readback"] == [
            {
                "sitelink_set_id": 5001,
                "title": "Цены",
                "href": "https://example.ru/?utm_source=yandex&utm_medium=cpc&utm_campaign=apply-slug#prices",
            }
        ]

    def test_sitelinks_api_error_fails_closed(self):
        settings = _settings("live_write")

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            service = request.url.path.rsplit("/", 1)[-1]
            if service == "ads" and body.get("method") in {"get", "update"}:
                return httpx.Response(200, json={"result": {"Ads": [_ad_with_sitelink_set()]}})
            if service == "sitelinks" and body.get("method") == "get":
                return httpx.Response(200, json={"result": {"SitelinksSets": [_sitelink_set(prices_href="https://example.ru/#prices")]}})
            if service == "sitelinks" and body.get("method") == "update":
                return httpx.Response(200, json={"error": {"error_code": 8000, "error_string": "bad request"}})
            raise AssertionError(f"unexpected request {request.url} {body}")

        yandex = _make_client(settings, handler)
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            resp = client.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-sl-fail-001",
                    "dry_run": False,
                    "campaign_slug": "fail-slug",
                    "include_sitelinks": True,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 502
        assert "applied" not in resp.text.lower()
        assert "placeholder-token" not in resp.text

    def test_apply_sends_full_sitelink_set_and_preserves_unchanged_items(self):
        settings = _settings("live_write")
        state: dict[str, Any] = {"sitelinks_updated": False, "sitelinks_update_payload": None}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            service = request.url.path.rsplit("/", 1)[-1]
            method = body.get("method")
            if service == "ads" and method == "get":
                return httpx.Response(200, json={"result": {"Ads": [_ad_with_sitelink_set()]}})
            if service == "ads" and method == "update":
                return httpx.Response(200, json={"result": {}})
            if service == "sitelinks" and method == "get":
                if state["sitelinks_updated"]:
                    return httpx.Response(
                        200,
                        json={
                            "result": {
                                "SitelinksSets": [
                                    _sitelink_set(
                                        prices_href=(
                                            "https://example.ru/?utm_source=yandex&utm_medium=cpc"
                                            "&utm_campaign=partial-slug#prices"
                                        ),
                                        reviews_href=(
                                            "https://example.ru/reviews?utm_source=yandex"
                                            "&utm_medium=cpc&utm_campaign=old#reviews"
                                        ),
                                    )
                                ]
                            }
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "SitelinksSets": [
                                _sitelink_set(
                                    prices_href="https://example.ru/#prices",
                                    reviews_href=(
                                        "https://example.ru/reviews?utm_source=yandex"
                                        "&utm_medium=cpc&utm_campaign=old#reviews"
                                    ),
                                )
                            ]
                        }
                    },
                )
            if service == "sitelinks" and method == "update":
                state["sitelinks_updated"] = True
                state["sitelinks_update_payload"] = body
                return httpx.Response(200, json={"result": {}})
            raise AssertionError(f"unexpected request {request.url} {body}")

        yandex = _make_client(settings, handler)
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            resp = client.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-sl-full-001",
                    "dry_run": False,
                    "campaign_slug": "partial-slug",
                    "include_sitelinks": True,
                    "overwrite": False,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200, resp.text
        sent_links = state["sitelinks_update_payload"]["params"]["SitelinksSets"][0]["Sitelinks"]
        assert [link["Title"] for link in sent_links] == ["Цены", "Отзывы"]
        assert "utm_campaign=partial-slug" in sent_links[0]["Href"]
        assert sent_links[1]["Href"] == "https://example.ru/reviews?utm_source=yandex&utm_medium=cpc&utm_campaign=old#reviews"
        assert sent_links[1]["Description"] == "Отзывы клиентов"

    def test_sitelinks_readback_failure_fails_closed(self):
        settings = _settings("live_write")
        state: dict[str, bool] = {"sitelinks_updated": False}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            service = request.url.path.rsplit("/", 1)[-1]
            if service == "ads" and body.get("method") in {"get", "update"}:
                return httpx.Response(200, json={"result": {"Ads": [_ad_with_sitelink_set()]}})
            if service == "sitelinks" and body.get("method") == "get":
                if state["sitelinks_updated"]:
                    return httpx.Response(200, json={"error": {"error_code": 5000, "error_string": "readback failed"}})
                return httpx.Response(200, json={"result": {"SitelinksSets": [_sitelink_set(prices_href="https://example.ru/#prices")]}})
            if service == "sitelinks" and body.get("method") == "update":
                state["sitelinks_updated"] = True
                return httpx.Response(200, json={"result": {}})
            raise AssertionError(f"unexpected request {request.url} {body}")

        yandex = _make_client(settings, handler)
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            resp = client.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-sl-rb-fail-001",
                    "dry_run": False,
                    "campaign_slug": "rb-fail-slug",
                    "include_sitelinks": True,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 502
        assert "applied" not in resp.text.lower()
        assert "placeholder-token" not in resp.text

    def test_sitelinks_partial_readback_missing_set_fails_closed(self):
        settings = _settings("live_write")
        state: dict[str, bool] = {"sitelinks_updated": False}
        second_ad = _ad_with_sitelink_set()
        second_ad["Id"] = 102
        second_ad["TextAd"] = dict(second_ad["TextAd"])
        second_ad["TextAd"]["SitelinkSetId"] = 5002

        def set_for(set_id: int, href: str) -> dict[str, Any]:
            item = _sitelink_set(prices_href=href)
            item["Id"] = set_id
            return item

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            service = request.url.path.rsplit("/", 1)[-1]
            if service == "ads" and body.get("method") == "get":
                return httpx.Response(200, json={"result": {"Ads": [_ad_with_sitelink_set(), second_ad]}})
            if service == "ads" and body.get("method") == "update":
                return httpx.Response(200, json={"result": {}})
            if service == "sitelinks" and body.get("method") == "get":
                if state["sitelinks_updated"]:
                    # Missing set 5002 in readback must fail closed.
                    return httpx.Response(
                        200,
                        json={"result": {"SitelinksSets": [set_for(5001, "https://example.ru/?utm_source=yandex&utm_medium=cpc&utm_campaign=partial-rb#prices")]}},
                    )
                return httpx.Response(
                    200,
                    json={"result": {"SitelinksSets": [set_for(5001, "https://example.ru/a#prices"), set_for(5002, "https://example.ru/b#prices")]}},
                )
            if service == "sitelinks" and body.get("method") == "update":
                state["sitelinks_updated"] = True
                return httpx.Response(200, json={"result": {}})
            raise AssertionError(f"unexpected request {request.url} {body}")

        yandex = _make_client(settings, handler)
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            resp = client.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-sl-partial-rb-001",
                    "dry_run": False,
                    "campaign_slug": "partial-rb",
                    "include_sitelinks": True,
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 502
        assert "applied" not in resp.text.lower()
        assert "placeholder-token" not in resp.text
