from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.store import store


class _LandingPreviewClient:
    def __init__(self) -> None:
        self.read_calls: list[tuple[str, list[int]]] = []
        self.update_calls: list[list[dict[str, Any]]] = []

    def ads_get_by_campaign_and_ids(self, campaign_id: str, ad_ids: list[int]) -> dict[str, Any]:
        self.read_calls.append((campaign_id, ad_ids))
        return {
            "ok": True,
            "result": {
                "Ads": [
                    {
                        "Id": 101,
                        "CampaignId": int(campaign_id),
                        "Type": "TEXT_AD",
                        "TextAd": {
                            "Title": "unchanged title",
                            "Text": "unchanged text",
                            "Href": "https://old.example.test/a",
                            "SitelinkSetId": 401,
                        },
                    }
                ]
            },
        }

    def ads_update(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        self.update_calls.append(items)
        raise AssertionError("dry-run must not call ads.update")


def _settings(mode: str = "live_readonly") -> Settings:
    return Settings(
        _env_file=None,
        directpilot_mode=mode,
        yandex_oauth_token="test-token",
        url_migration_allowed_hosts="new.example.test",
    )


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
        lambda _url: httpx.Response(200, text="<html><body>ok</body></html>"),
        raising=False,
    )


def test_landing_url_request_defaults_to_preview_and_rejects_duplicate_ad_ids() -> None:
    from app.models import LandingUrlMigrationRequest

    preview = LandingUrlMigrationRequest(
        items=[
            {
                "ad_id": 101,
                "expected_href": "https://old.example.test/a",
                "target_href": "https://new.example.test/a",
            }
        ]
    )
    assert preview.dry_run is True
    assert preview.approved is False

    with pytest.raises(ValidationError):
        LandingUrlMigrationRequest(
            items=[
                {
                    "ad_id": 101,
                    "expected_href": "https://old.example.test/a",
                    "target_href": "https://new.example.test/a",
                },
                {
                    "ad_id": 101,
                    "expected_href": "https://old.example.test/b",
                    "target_href": "https://new.example.test/b",
                },
            ]
        )

    with pytest.raises(ValidationError):
        LandingUrlMigrationRequest.model_validate(
            {
                "items": [
                    {
                        "ad_id": 101,
                        "expected_href": "https://old.example.test/a",
                        "target_href": "https://new.example.test/a",
                    }
                ],
                "unexpected": True,
            }
        )


def test_landing_url_dry_run_reads_validates_and_previews_minimal_update(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _LandingPreviewClient()
    _install_public_url_fakes(monkeypatch)
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = TestClient(app).post(
            "/yandex/campaigns/713397771/ads/landing-urls",
            json={
                "items": [
                    {
                        "ad_id": 101,
                        "expected_href": "https://old.example.test/a",
                        "target_href": "https://new.example.test/a",
                    }
                ]
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["completed"] is False
    assert body["changes"] == [
        {
            "entity_type": "ad",
            "entity_id": "101",
            "before_href": "https://old.example.test/a",
            "after_href": "https://new.example.test/a",
        }
    ]
    assert body["payload_preview"]["ads.update"] == {
        "method": "update",
        "params": {"Ads": [{"Id": 101, "TextAd": {"Href": "https://new.example.test/a"}}]},
    }
    assert fake.read_calls == [("713397771", [101])]
    assert fake.update_calls == []


def test_target_validation_rejects_private_dns_unsafe_redirect_missing_anchor_and_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_settings = _settings()
    monkeypatch.setattr(
        store,
        "_url_migration_dns_resolver",
        lambda _host, _port: ["127.0.0.1"],
        raising=False,
    )
    with pytest.raises(ValueError, match="public IP"):
        store._validate_url_migration_target(
            "https://new.example.test/page", settings=private_settings, require_anchor=False
        )

    monkeypatch.setattr(
        store,
        "_url_migration_dns_resolver",
        lambda _host, _port: ["93.184.216.34"],
        raising=False,
    )
    monkeypatch.setattr(
        store,
        "_url_migration_http_fetcher",
        lambda _url: httpx.Response(302, headers={"location": "https://evil.example.test/"}),
        raising=False,
    )
    with pytest.raises(ValueError, match="allowlist|unexpected host"):
        store._validate_url_migration_target(
            "https://new.example.test/page", settings=private_settings, require_anchor=False
        )

    monkeypatch.setattr(
        store,
        "_url_migration_http_fetcher",
        lambda _url: httpx.Response(200, text="<main id='different'></main>"),
        raising=False,
    )
    with pytest.raises(ValueError, match="fragment anchor"):
        store._validate_url_migration_target(
            "https://new.example.test/page#required", settings=private_settings, require_anchor=True
        )

    monkeypatch.setattr(
        store,
        "_url_migration_http_fetcher",
        lambda _url: httpx.Response(404, text="not found"),
        raising=False,
    )
    with pytest.raises(ValueError, match="successful public HTTP"):
        store._validate_url_migration_target(
            "https://new.example.test/page", settings=private_settings, require_anchor=False
        )


def test_target_validation_normalizes_idn_and_requires_query_before_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        directpilot_mode="live_readonly",
        yandex_oauth_token="test-token",
        url_migration_allowed_hosts="xn-----8kcahqsjoqlhez8a.xn--p1ai",
    )
    monkeypatch.setattr(
        store,
        "_url_migration_dns_resolver",
        lambda _host, _port: ["93.184.216.34"],
        raising=False,
    )
    requested_urls: list[str] = []
    monkeypatch.setattr(
        store,
        "_url_migration_http_fetcher",
        lambda url: (
            requested_urls.append(url)
            or httpx.Response(200, text="<main id='problems'></main>")
        ),
        raising=False,
    )

    canonical = "https://ремонт-в-Казани.рф/stiralnye-mashiny/?utm_source=yandex#problems"
    result = store._validate_url_migration_target(canonical, settings=settings, require_anchor=True)

    assert result["submitted_href"] == canonical
    assert result["normalized_host"] == "xn-----8kcahqsjoqlhez8a.xn--p1ai"
    assert requested_urls == [
        "https://ремонт-в-Казани.рф/stiralnye-mashiny/?utm_source=yandex"
    ]
    with pytest.raises(ValueError, match="query parameters must precede fragment"):
        store._validate_url_migration_target(
            "https://ремонт-в-Казани.рф/stiralnye-mashiny/#problems?utm_source=yandex",
            settings=settings,
            require_anchor=True,
        )


class _LandingApplyClient:
    def __init__(self) -> None:
        self.ad = {
            "Id": 101,
            "CampaignId": 713397771,
            "Type": "TEXT_AD",
            "TextAd": {
                "Title": "must remain",
                "Text": "must remain",
                "Href": "https://old.example.test/a?utm_content=one",
                "SitelinkSetId": 401,
            },
        }
        self.update_calls: list[list[dict[str, Any]]] = []

    def ads_get_by_campaign_and_ids(self, campaign_id: str, ad_ids: list[int]) -> dict[str, Any]:
        assert campaign_id == "713397771"
        assert ad_ids == [101]
        return {"ok": True, "result": {"Ads": [self.ad]}}

    def ads_update(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        self.update_calls.append(items)
        self.ad["TextAd"].update(items[0]["TextAd"])
        return {"ok": True, "result": {"UpdateResults": [{"Id": 101}]}}


def test_landing_url_apply_uses_only_minimal_href_payload_and_confirms_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _LandingApplyClient()
    _install_public_url_fakes(monkeypatch)
    monkeypatch.setattr(store, "_url_migration_results_by_key", {}, raising=False)
    app.dependency_overrides[get_settings] = lambda: _settings("live_write")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = TestClient(app).post(
            "/yandex/campaigns/713397771/ads/landing-urls",
            json={
                "items": [
                    {
                        "ad_id": 101,
                        "expected_href": "https://old.example.test/a?utm_content=one",
                        "target_href": "https://new.example.test/a?utm_content=one",
                    }
                ],
                "dry_run": False,
                "approved": True,
                "idempotency_key": "landing-apply-001",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["completed"] is True
    assert body["applied"] is True
    assert body["partial_failure"] is False
    assert fake.update_calls == [[{"Id": 101, "TextAd": {"Href": "https://new.example.test/a?utm_content=one"}}]]
    assert body["readback"] == [
        {
            "ad_id": 101,
            "campaign_id": "713397771",
            "type": "TEXT_AD",
            "href": "https://new.example.test/a?utm_content=one",
            "sitelink_set_id": 401,
        }
    ]


def test_landing_url_apply_blocks_missing_target_fragment_before_provider_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _LandingApplyClient()
    _install_public_url_fakes(monkeypatch)
    monkeypatch.setattr(store, "_url_migration_results_by_key", {}, raising=False)
    app.dependency_overrides[get_settings] = lambda: _settings("live_write")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = TestClient(app).post(
            "/yandex/campaigns/713397771/ads/landing-urls",
            json={
                "items": [
                    {
                        "ad_id": 101,
                        "expected_href": "https://old.example.test/a?utm_content=one",
                        "target_href": "https://new.example.test/a#required-anchor",
                    }
                ],
                "dry_run": False,
                "approved": True,
                "idempotency_key": "landing-fragment-missing-001",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    assert "fragment anchor" in response.json()["detail"]
    assert fake.update_calls == []


def test_landing_url_apply_accepts_existing_target_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _LandingApplyClient()
    monkeypatch.setattr(
        store,
        "_url_migration_dns_resolver",
        lambda _host, _port: ["93.184.216.34"],
        raising=False,
    )
    monkeypatch.setattr(
        store,
        "_url_migration_http_fetcher",
        lambda _url: httpx.Response(200, text="<main id='required-anchor'></main>"),
        raising=False,
    )
    monkeypatch.setattr(store, "_url_migration_results_by_key", {}, raising=False)
    app.dependency_overrides[get_settings] = lambda: _settings("live_write")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = TestClient(app).post(
            "/yandex/campaigns/713397771/ads/landing-urls",
            json={
                "items": [
                    {
                        "ad_id": 101,
                        "expected_href": "https://old.example.test/a?utm_content=one",
                        "target_href": "https://new.example.test/a#required-anchor",
                    }
                ],
                "dry_run": False,
                "approved": True,
                "idempotency_key": "landing-fragment-existing-001",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert fake.update_calls == [
        [{"Id": 101, "TextAd": {"Href": "https://new.example.test/a#required-anchor"}}]
    ]


@pytest.mark.parametrize(
    "target_href",
    [
        "http://new.example.test/insecure",
        "https://user:password@new.example.test/private",
    ],
    ids=["https-required", "credentials-forbidden"],
)
def test_landing_url_preflight_rejects_insecure_or_credentialed_target_before_reads(
    monkeypatch: pytest.MonkeyPatch,
    target_href: str,
) -> None:
    fake = _LandingPreviewClient()
    _install_public_url_fakes(monkeypatch)
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = TestClient(app).post(
            "/yandex/campaigns/713397771/ads/landing-urls",
            json={
                "items": [
                    {
                        "ad_id": 101,
                        "expected_href": "https://old.example.test/a",
                        "target_href": target_href,
                    }
                ]
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert fake.read_calls == []
    assert fake.update_calls == []


class _LimitedLandingReadClient(_LandingPreviewClient):
    def ads_get_by_campaign_and_ids(self, campaign_id: str, ad_ids: list[int]) -> dict[str, Any]:
        response = super().ads_get_by_campaign_and_ids(campaign_id, ad_ids)
        response["result"]["LimitedBy"] = 1
        return response


def test_landing_url_preflight_fails_closed_when_campaign_scoped_read_is_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _LimitedLandingReadClient()
    _install_public_url_fakes(monkeypatch)
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = TestClient(app).post(
            "/yandex/campaigns/713397771/ads/landing-urls",
            json={
                "items": [
                    {
                        "ad_id": 101,
                        "expected_href": "https://old.example.test/a",
                        "target_href": "https://new.example.test/a",
                    }
                ]
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert fake.update_calls == []


@pytest.mark.parametrize(
    ("settings", "request_overrides", "expected_detail"),
    [
        (
            _settings("live_readonly"),
            {"dry_run": False, "approved": True, "idempotency_key": "gate-mode-001"},
            "Live writes require",
        ),
        (
            _settings("live_write"),
            {"dry_run": False, "approved": False, "idempotency_key": "gate-approval-001"},
            "explicit approval",
        ),
        (
            _settings("live_write"),
            {"dry_run": False, "approved": True},
            "idempotency_key",
        ),
    ],
    ids=["live-write-mode", "explicit-approval", "idempotency-key"],
)
def test_landing_apply_requires_all_write_gates_before_provider_reads_or_writes(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    request_overrides: dict[str, Any],
    expected_detail: str,
) -> None:
    fake = _LandingPreviewClient()
    _install_public_url_fakes(monkeypatch)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = TestClient(app).post(
            "/yandex/campaigns/713397771/ads/landing-urls",
            json={
                "items": [
                    {
                        "ad_id": 101,
                        "expected_href": "https://old.example.test/a",
                        "target_href": "https://new.example.test/a",
                    }
                ],
                **request_overrides,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    assert expected_detail in response.json()["detail"]
    assert fake.read_calls == []
    assert fake.update_calls == []


def test_landing_endpoint_rejects_duplicate_ad_ids_before_provider_access() -> None:
    fake = _LandingPreviewClient()
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = TestClient(app).post(
            "/yandex/campaigns/713397771/ads/landing-urls",
            json={
                "items": [
                    {
                        "ad_id": 101,
                        "expected_href": "https://old.example.test/a",
                        "target_href": "https://new.example.test/a",
                    },
                    {
                        "ad_id": 101,
                        "expected_href": "https://old.example.test/b",
                        "target_href": "https://new.example.test/b",
                    },
                ]
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "duplicate ad_id" in response.text
    assert fake.read_calls == []
    assert fake.update_calls == []
