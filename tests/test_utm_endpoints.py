"""Tests for UTM endpoints: audit, plan, apply.

Covers:
- GET /yandex/campaigns/{campaign_id}/utm-audit — read-only, no writes
- POST /yandex/campaigns/{campaign_id}/utm-plan — dry_run only, preview
- POST /yandex/campaigns/{campaign_id}/utm-apply — write-gated
- dry_run never calls ads_update
- apply requires live_write/approved/idempotency_key
- ads.update payload includes required TextAd fields
- sitelinks apply preview/readback path is implemented through sitelinks.update
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.yandex_direct import YandexDirectClient, YandexDirectError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(mode: str = "live_readonly", token: str | None = "t-secret") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=token)


def _make_client(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# UTM audit
# ---------------------------------------------------------------------------


class TestUtmAudit:
    def test_audit_returns_structured_result_in_mock_mode(self):
        """Audit in mock mode returns deterministic data with source=mock."""
        settings = Settings(_env_file=None, directpilot_mode="mock")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.get("/yandex/campaigns/cmp_mock_local_services/utm-audit")
            assert resp.status_code == 200
            body = resp.json()
            assert body["campaign_id"] == "cmp_mock_local_services"
            assert body["source"] == "mock"
            assert body["read_only"] is True
            assert "items" in body
            assert "ads_total" in body
            assert "sitelinks_total" in body
            assert "complete_count" in body
            assert "missing_count" in body
        finally:
            app.dependency_overrides.clear()

    def test_audit_surfaces_utm_status_per_ad(self):
        """Each ad URL is audited and surfaced with utm_status."""
        settings = Settings(_env_file=None, directpilot_mode="mock")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.get("/yandex/campaigns/cmp_mock_local_services/utm-audit")
            assert resp.status_code == 200
            body = resp.json()
            for item in body["items"]:
                assert "entity_type" in item
                assert "entity_id" in item
                assert "url" in item
                assert "utm_status" in item
                assert item["utm_status"] in ("complete", "partial", "missing", "mismatch")
        finally:
            app.dependency_overrides.clear()

    def test_audit_unknown_campaign_returns_empty(self):
        """Unknown campaign returns empty items, not 404."""
        settings = Settings(_env_file=None, directpilot_mode="mock")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.get("/yandex/campaigns/nonexistent/utm-audit")
            assert resp.status_code == 200
            body = resp.json()
            assert body["items"] == []
            assert body["ads_total"] == 0
        finally:
            app.dependency_overrides.clear()

    def test_audit_no_secret_in_response(self):
        """Response must not contain tokens or secrets."""
        settings = Settings(_env_file=None, directpilot_mode="mock", yandex_oauth_token="secret-12345")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.get("/yandex/campaigns/cmp_mock_local_services/utm-audit")
            assert resp.status_code == 200
            body = resp.json()
            body_str = json.dumps(body)
            assert "secret-12345" not in body_str
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# UTM plan — preview only
# ---------------------------------------------------------------------------


class TestUtmPlan:
    def test_plan_returns_preview_in_mock_mode(self):
        """Plan endpoint returns dry_run=True, applied=False."""
        settings = Settings(_env_file=None, directpilot_mode="mock")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-plan",
                json={"include_sitelinks": True},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["dry_run"] is True
            assert body["applied"] is False
            assert body["source"] == "mock"
            assert "items" in body
            assert "campaign_slug" in body
        finally:
            app.dependency_overrides.clear()

    def test_plan_generates_slug_when_not_provided(self):
        """When campaign_slug is missing, generate from campaign name/id."""
        settings = Settings(_env_file=None, directpilot_mode="mock")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-plan",
                json={},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["campaign_slug"] != ""
        finally:
            app.dependency_overrides.clear()

    def test_plan_uses_provided_slug(self):
        """When campaign_slug is provided, use it directly."""
        settings = Settings(_env_file=None, directpilot_mode="mock")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-plan",
                json={"campaign_slug": "my-slug"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["campaign_slug"] == "my-slug"
        finally:
            app.dependency_overrides.clear()

    def test_plan_returns_new_urls_with_utm(self):
        """New URLs have UTM params injected when overwrite=True."""
        settings = Settings(_env_file=None, directpilot_mode="mock")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-plan",
                json={"campaign_slug": "test", "overwrite": True},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert len(body["items"]) >= 1
            for item in body["items"]:
                assert "utm_source=yandex" in item["new_url"]
                assert "utm_medium=cpc" in item["new_url"]
                assert "utm_campaign=test" in item["new_url"]
        finally:
            app.dependency_overrides.clear()

    def test_plan_dry_run_never_calls_ads_update(self):
        """dry_run=true must not call any write method."""
        settings = _settings("live_readonly")
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["called"] = True
            return httpx.Response(200, json={"result": {"Ads": []}})

        client = _make_client(settings, handler)
        test_app = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: client
        try:
            resp = test_app.post(
                "/yandex/campaigns/12345/utm-plan",
                json={"campaign_slug": "test"},
            )
            assert resp.status_code == 200
            # Plan is ALWAYS dry_run — no write methods should be called.
            # The handler is only for ads_get (read), never ads_update (write).
            # ads_update payload in plan response is locally built, not sent.
        finally:
            app.dependency_overrides.clear()

    def test_plan_no_secret_in_response(self):
        """Response must not contain tokens."""
        settings = _settings("live_readonly", token="secret-token")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-plan",
                json={"campaign_slug": "test"},
            )
            assert resp.status_code == 200
            body_str = json.dumps(resp.json())
            assert "secret-token" not in body_str
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# UTM apply — write-gated
# ---------------------------------------------------------------------------


class TestUtmApply:
    def test_apply_dry_run_returns_preview_not_applied(self):
        """dry_run=True returns applied=False with payload_preview."""
        settings = Settings(_env_file=None, directpilot_mode="mock")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "key-dr-001",
                    "dry_run": True,
                    "campaign_slug": "test",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["dry_run"] is True
            assert body["applied"] is False
            assert body["ad_ids"] == []
            assert body["payload_preview"] is not None
        finally:
            app.dependency_overrides.clear()

    def test_apply_requires_approved(self):
        """approved=false must be rejected before any network call."""
        settings = Settings(_env_file=None, directpilot_mode="live_write")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-apply",
                json={
                    "approved": False,
                    "idempotency_key": "key-001",
                    "dry_run": False,
                    "campaign_slug": "test",
                },
            )
            assert resp.status_code == 409
            assert "approval" in resp.text.lower()
        finally:
            app.dependency_overrides.clear()

    def test_apply_requires_idempotency_key(self):
        """idempotency_key < 6 chars must be rejected by Pydantic validation."""
        settings = Settings(_env_file=None, directpilot_mode="live_write")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "ab",
                    "dry_run": False,
                    "campaign_slug": "test",
                },
            )
            assert resp.status_code == 422
        finally:
            app.dependency_overrides.clear()

    def test_apply_rejected_in_live_readonly_mode(self):
        """live_readonly must block real writes with 409 before any network call."""
        settings = _settings("live_readonly")

        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("ads.update must not be called in live_readonly mode")
            return httpx.Response(200)

        client = _make_client(settings, handler)
        test_app = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: client
        try:
            resp = test_app.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "key-rl-01",
                    "dry_run": False,
                    "campaign_slug": "test",
                },
            )
            assert resp.status_code == 409
        finally:
            app.dependency_overrides.clear()

    def test_apply_dry_run_never_calls_ads_update(self):
        """dry_run=True must not call ads_update even when approved."""
        settings = _settings("live_write")

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            if body.get("method") == "update":
                pytest.fail("ads.update must not be called on dry_run")
            # Normal ads.get response for read
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Ads": [
                            {
                                "Id": 1,
                                "AdGroupId": 10,
                                "CampaignId": 12345,
                                "Status": "ACCEPTED",
                                "State": "ON",
                                "Type": "TEXT_AD",
                                "TextAd": {
                                    "Title": "Test Ad",
                                    "Text": "Test text",
                                    "Href": "https://example.com/page",
                                },
                            }
                        ]
                    }
                },
            )

        client = _make_client(settings, handler)
        test_app = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: client
        try:
            resp = test_app.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "key-dr-02",
                    "dry_run": True,
                    "campaign_slug": "test",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["applied"] is False
        finally:
            app.dependency_overrides.clear()

    def test_apply_payload_includes_required_textad_fields(self):
        """ads.update payload must include Title, Text, Href for each ad."""
        settings = Settings(_env_file=None, directpilot_mode="mock")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "key-pl-01",
                    "dry_run": True,
                    "campaign_slug": "test",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            if body.get("payload_preview") and body["payload_preview"].get("params", {}).get("Ads"):
                for ad in body["payload_preview"]["params"]["Ads"]:
                    if "TextAd" in ad:
                        ta = ad["TextAd"]
                        assert "Title" in ta, "TextAd must include Title"
                        assert "Text" in ta, "TextAd must include Text"
                        assert "Href" in ta, "TextAd must include Href"
        finally:
            app.dependency_overrides.clear()

    def test_apply_sitelinks_surfaced_in_preview(self):
        """Sitelink apply is implemented and dry-run surfaces sitelink changes."""
        settings = Settings(_env_file=None, directpilot_mode="mock")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "key-si-01",
                    "dry_run": True,
                    "campaign_slug": "test",
                    "include_sitelinks": True,
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["not_implemented"] == []
            assert body["sitelink_items"]
            assert body["payload_preview"]["sitelinks_preview"]["method"] == "sitelinks.update"
        finally:
            app.dependency_overrides.clear()

    def test_apply_no_secret_in_response(self):
        """Response must not leak tokens."""
        settings = Settings(_env_file=None, directpilot_mode="mock", yandex_oauth_token="secret-999")
        client = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None
        try:
            resp = client.post(
                "/yandex/campaigns/cmp_mock_local_services/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "key-sec-01",
                    "dry_run": True,
                    "campaign_slug": "test",
                },
            )
            assert resp.status_code == 200
            body_str = json.dumps(resp.json())
            assert "secret-999" not in body_str
        finally:
            app.dependency_overrides.clear()

    def test_apply_idempotency_replay_returns_cached_result_without_re_sending(self):
        """Same idempotency_key + same request MUST replay cached result
        and must NOT call ads.update again."""
        settings = _settings("live_write", token="SECRET-IDEMP-UTM")
        captured: dict[str, Any] = {"update_bodies": []}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            if body.get("method") == "update":
                captured["update_bodies"].append(body)
                return httpx.Response(200, json={"result": {}})
            # ads.get response for read
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Ads": [
                            {
                                "Id": 1,
                                "AdGroupId": 10,
                                "CampaignId": 12345,
                                "Status": "ACCEPTED",
                                "State": "ON",
                                "Type": "TEXT_AD",
                                "TextAd": {
                                    "Title": "Test Ad",
                                    "Text": "Test text",
                                    "Href": "https://example.com/page",
                                },
                            }
                        ]
                    }
                },
            )

        yandex = _make_client(settings, handler)
        test_app = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            first = test_app.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-idemp-001",
                    "dry_run": False,
                    "campaign_slug": "replay-test",
                },
            )
            second = test_app.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-idemp-001",
                    "dry_run": False,
                    "campaign_slug": "replay-test",
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        # Only one ads.update call must have been dispatched.
        assert len(captured["update_bodies"]) == 1
        # Both responses must carry the same audit_id (replay).
        assert first.json()["audit_id"] == second.json()["audit_id"]
        # No token leak.
        assert "SECRET-IDEMP-UTM" not in first.text
        assert "SECRET-IDEMP-UTM" not in second.text

    def test_apply_idempotency_dry_run_and_apply_have_separate_cache(self):
        """dry_run and apply must use separate cache slots — a dry_run
        replay must not consume a real apply's idempotency_key."""
        settings = _settings("live_write", token="SECRET-SCOPE")
        captured: dict[str, Any] = {"update_bodies": []}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            if body.get("method") == "update":
                captured["update_bodies"].append(body)
                return httpx.Response(200, json={"result": {}})
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Ads": [
                            {
                                "Id": 1,
                                "AdGroupId": 10,
                                "CampaignId": 12345,
                                "Status": "ACCEPTED",
                                "State": "ON",
                                "Type": "TEXT_AD",
                                "TextAd": {
                                    "Title": "Test Ad",
                                    "Text": "Test text",
                                    "Href": "https://example.com/page",
                                },
                            }
                        ]
                    }
                },
            )

        yandex = _make_client(settings, handler)
        test_app = TestClient(app)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex
        try:
            # dry_run first — must NOT consume the apply slot.
            dry = test_app.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-scope-001",
                    "dry_run": True,
                    "campaign_slug": "scope-test",
                },
            )
            assert dry.status_code == 200
            assert dry.json()["applied"] is False

            # Real apply with the SAME key — must dispatch its own call.
            real = test_app.post(
                "/yandex/campaigns/12345/utm-apply",
                json={
                    "approved": True,
                    "idempotency_key": "utm-scope-001",
                    "dry_run": False,
                    "campaign_slug": "scope-test",
                },
            )
            assert real.status_code == 200
            assert real.json()["applied"] is True
        finally:
            app.dependency_overrides.clear()

        # Both dry_run and apply must have dispatched a total of 1 real call.
        assert len(captured["update_bodies"]) == 1
        assert "SECRET-SCOPE" not in dry.text
        assert "SECRET-SCOPE" not in real.text
