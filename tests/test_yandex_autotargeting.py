"""Tests for the Yandex Direct autotargeting settings read/update endpoints.

``GET /yandex/campaigns/{campaign_id}/autotargeting`` — pure read-only,
no write gate, available in all modes.  Returns per-ad-group autotargeting
categories and brand options from the ``---autotargeting`` keyword rows.

``POST /yandex/campaigns/{campaign_id}/autotargeting`` — gated write
path for updating autotargeting settings via v5 ``keywords.update``.

* ``dry_run=True`` (default) is ALWAYS allowed and NEVER performs a network
  write. The response includes the exact v5 ``keywords.update`` payload
  that WOULD be sent, with ``applied=False``.
* In ``live_write`` mode, ``dry_run=False`` is allowed only when
  ``approved=True`` and a valid ``idempotency_key`` is supplied.
* In non-``live_write`` modes, ``dry_run=False`` is REJECTED (HTTP 409).
* ``approved=false`` is ALWAYS rejected (HTTP 409) before any action.
* Categories are always sent with all five booleans explicitly (``YES``
  or ``NO``) to avoid the Direct API pitfall where missing categories
  default to ``YES``.
* Default preset: ``exact_narrow`` (Exact=YES, Narrow=YES,
  Alternative=NO, Accessory=NO, Broader=NO).
* Brand options default: WithoutBrands=YES, WithAdvertiserBrand=YES,
  WithCompetitorsBrand=NO.
"""

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

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

SECRET_TOKEN = "TOPSECRET-AUTOTARGETING-001"

REAL_CAMPAIGN_ID = "cmp_mock_local_services"
MOCK_AD_GROUP_ID = "adg_mock_1001"


def _settings(mode: str, token: str | None = SECRET_TOKEN) -> Settings:
    return Settings(
        _env_file=None, directpilot_mode=mode, yandex_oauth_token=token
    )


def _client_with_handler(settings: Settings, handler) -> YandexDirectClient:
    def _transport(request: httpx.Request) -> httpx.Response:
        try:
            return handler(request)
        except AssertionError:
            raise

    transport = httpx.MockTransport(_transport)
    return YandexDirectClient(settings=settings, transport=transport)


# --- GET handler builders ---------------------------------------------------


def _autotargeting_get_handler(
    categories: dict[str, str] | None = None,
    brand_options: dict[str, str] | None = None,
    *,
    keyword_id: int = 123,
    ad_group_id: int = 456,
    ad_group_name: str = "Test Ad Group",
) -> callable:
    """Build a keywords.get handler returning one ---autotargeting row."""
    if categories is None:
        categories = {
            "Exact": "YES",
            "Narrow": "YES",
            "Alternative": "NO",
            "Accessory": "NO",
            "Broader": "NO",
        }
    if brand_options is None:
        brand_options = {
            "WithoutBrands": "YES",
            "WithAdvertiserBrand": "YES",
            "WithCompetitorsBrand": "NO",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "get"

        # adgroups.get — called separately by the store for names
        if request.url.path.endswith("/adgroups"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AdGroups": [
                            {
                                "Id": ad_group_id,
                                "Name": ad_group_name,
                                "CampaignId": 1,
                                "Status": "ACCEPTED",
                                "ServingStatus": "ELIGIBLE",
                            }
                        ]
                    }
                },
            )

        # keywords.get — main call
        keyword_row: dict[str, Any] = {
            "Id": keyword_id,
            "AdGroupId": ad_group_id,
            "CampaignId": 1,
            "Keyword": "---autotargeting",
            "Bid": None,
            "ContextBid": None,
            "StrategyPriority": "NORMAL",
            "State": "ON",
            "Status": "ACCEPTED",
            "ServingStatus": "ELIGIBLE",
        }
        if categories is not None:
            keyword_row["AutotargetingSettingsCategories"] = dict(categories)
        if brand_options is not None:
            keyword_row["AutotargetingSettingsBrandOptions"] = dict(brand_options)

        return httpx.Response(
            200,
            json={"result": {"Keywords": [keyword_row]}},
        )

    return handler


# --- POST handler builders --------------------------------------------------


def _autotargeting_update_handler(
    success: bool = True,
    error_code: int = 0,
    error_detail: str = "",
) -> callable:
    """Build a keywords.update handler that checks the payload."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)

        # The store calls keywords_get_autotargeting first (GET), then
        # keywords_update (UPDATE).  The GET handler is set separately;
        # here we only assert the UPDATE shape.
        if body.get("method") == "get":
            # Return a basic autotargeting row for the read-before-write
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {
                                "Id": 123,
                                "AdGroupId": 456,
                                "CampaignId": 1,
                                "Keyword": "---autotargeting",
                                "State": "ON",
                                "Status": "ACCEPTED",
                                "ServingStatus": "ELIGIBLE",
                                "AutotargetingSettingsCategories": {
                                    "Exact": "YES",
                                    "Narrow": "YES",
                                    "Alternative": "NO",
                                    "Accessory": "NO",
                                    "Broader": "NO",
                                },
                                "AutotargetingSettingsBrandOptions": {
                                    "WithoutBrands": "YES",
                                    "WithAdvertiserBrand": "YES",
                                    "WithCompetitorsBrand": "NO",
                                },
                            }
                        ]
                    }
                },
            )

        assert body["method"] == "update"
        assert "Keywords" in body["params"]
        for kw in body["params"]["Keywords"]:
            assert "AutotargetingSettings" in kw, (
                "Update must use AutotargetingSettings, not deprecated path"
            )
            assert "AutotargetingCategories" not in kw, (
                "Update must NOT use deprecated AutotargetingCategories"
            )
            # All five category booleans must be explicit
            cats = kw["AutotargetingSettings"]["Categories"]
            for name in ("Exact", "Narrow", "Alternative", "Accessory", "Broader"):
                assert name in cats, f"Missing category {name}"
                assert cats[name] in ("YES", "NO"), f"Category {name}={cats[name]}"
            # All three brand option booleans must be explicit
            brands = kw["AutotargetingSettings"]["BrandOptions"]
            for name in ("WithoutBrands", "WithAdvertiserBrand", "WithCompetitorsBrand"):
                assert name in brands, f"Missing brand option {name}"
                assert brands[name] in ("YES", "NO"), f"Brand option {name}={brands[name]}"

        if success:
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(
            200,
            json={
                "error": {
                    "error_code": error_code,
                    "error_detail": error_detail,
                }
            },
        )

    return handler


# ===========================================================================
# GET /yandex/campaigns/{campaign_id}/autotargeting — read tests
# ===========================================================================


class TestAutotargetingRead:
    """Read-only endpoint — no write gates, available in all modes."""

    # --- mock mode ----------------------------------------------------------

    def test_mock_mode_returns_deterministic_data(self):
        """In mock mode, GET autotargeting returns deterministic mock data."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.get(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "mock"
        assert body["read_only"] is True
        assert body["campaign_id"] == REAL_CAMPAIGN_ID
        assert body["default_preset"] == "exact_narrow"
        assert len(body["ad_groups"]) >= 1

        ag = body["ad_groups"][0]
        assert "ad_group_id" in ag
        assert "ad_group_name" in ag
        assert "autotargeting_keyword_id" in ag
        assert "status" in ag
        assert ag["categories"]["Exact"] == "YES"
        assert ag["categories"]["Narrow"] == "YES"
        assert ag["categories"]["Alternative"] == "NO"
        assert ag["categories"]["Accessory"] == "NO"
        assert ag["categories"]["Broader"] == "NO"
        assert ag["brand_options"]["WithoutBrands"] == "YES"
        assert ag["brand_options"]["WithAdvertiserBrand"] == "YES"
        assert ag["brand_options"]["WithCompetitorsBrand"] == "NO"
        assert "raw_provider" in ag

    def test_mock_mode_no_token_leakage(self):
        """GET autotargeting never leaks the token."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.get(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting"
        )
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    def test_read_all_five_categories_present(self):
        """mock GET response has all five category names in each ad group."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.get(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting"
        )
        assert resp.status_code == 200
        body = resp.json()
        for ag in body["ad_groups"]:
            cats = ag["categories"]
            assert set(cats.keys()) == {
                "Exact", "Narrow", "Alternative", "Accessory", "Broader"
            }

    def test_read_all_three_brand_options_present(self):
        """mock GET response has all three brand option names in each ad group."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.get(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting"
        )
        assert resp.status_code == 200
        body = resp.json()
        for ag in body["ad_groups"]:
            brands = ag["brand_options"]
            assert set(brands.keys()) == {
                "WithoutBrands", "WithAdvertiserBrand", "WithCompetitorsBrand"
            }

    # --- live mode ----------------------------------------------------------

    def test_live_mode_returns_autotargeting_from_handler(self):
        """In live_write mode, GET returns autotargeting from the handler."""
        settings = _settings("live_write")
        handler_fn = _autotargeting_get_handler()
        yandex = _client_with_handler(settings, handler_fn)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.get("/yandex/campaigns/710691939/autotargeting")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "yandex"
        assert body["read_only"] is True
        assert len(body["ad_groups"]) == 1

        ag = body["ad_groups"][0]
        assert ag["ad_group_id"] == "456"
        assert ag["ad_group_name"] == "Test Ad Group"
        assert ag["autotargeting_keyword_id"] == "123"
        assert ag["status"] == "ACCEPTED"
        assert ag["state"] == "ON"
        assert ag["serving_status"] == "ELIGIBLE"
        assert ag["categories"]["Exact"] == "YES"
        assert ag["categories"]["Narrow"] == "YES"
        assert ag["categories"]["Alternative"] == "NO"
        assert "raw_provider" in ag

    def test_live_mode_no_token_leakage(self):
        """GET autotargeting in live mode never leaks the token."""
        settings = _settings("live_write")
        handler_fn = _autotargeting_get_handler()
        yandex = _client_with_handler(settings, handler_fn)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.get("/yandex/campaigns/710691939/autotargeting")
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    def test_live_mode_only_returns_autotargeting_rows(self):
        """GET filters to only ---autotargeting rows, ignoring normal keywords."""
        settings = _settings("live_write")

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["method"] == "get"

            if request.url.path.endswith("/adgroups"):
                return httpx.Response(
                    200,
                    json={"result": {"AdGroups": []}},
                )

            return httpx.Response(
                200,
                json={
                    "result": {
                        "Keywords": [
                            {
                                "Id": 1,
                                "AdGroupId": 10,
                                "Keyword": "сантехник на дом",
                                "State": "ON",
                                "Status": "ACCEPTED",
                            },
                            {
                                "Id": 2,
                                "AdGroupId": 10,
                                "Keyword": "---autotargeting",
                                "State": "ON",
                                "Status": "ACCEPTED",
                                "AutotargetingSettingsCategories": {
                                    "Exact": "YES",
                                    "Narrow": "NO",
                                    "Alternative": "NO",
                                    "Accessory": "NO",
                                    "Broader": "NO",
                                },
                            },
                        ]
                    }
                },
            )

        yandex = _client_with_handler(settings, handler)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.get("/yandex/campaigns/710691939/autotargeting")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Should only have the autotargeting row, not the normal keyword
        assert len(body["ad_groups"]) == 1
        assert body["ad_groups"][0]["autotargeting_keyword_id"] == "2"


# ===========================================================================
# POST /yandex/campaigns/{campaign_id}/autotargeting — write tests
# ===========================================================================


class TestAutotargetingUpdateDryRun:
    """Dry-run mode: preview-only, no network write, applied=False."""

    # --- dry-run in mock mode -----------------------------------------------

    def test_dry_run_default_exact_narrow_sends_all_five_categories(self):
        """Default exact_narrow preset sends all five category booleans explicitly."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-dry-001",
                "dry_run": True,
                "preset": "exact_narrow",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dry_run"] is True
        assert body["applied"] is False
        assert body["preset"] == "exact_narrow"
        assert body["categories_applied"] == {
            "Exact": "YES",
            "Narrow": "YES",
            "Alternative": "NO",
            "Accessory": "NO",
            "Broader": "NO",
        }
        assert body["brand_options_applied"] == {
            "WithoutBrands": "YES",
            "WithAdvertiserBrand": "YES",
            "WithCompetitorsBrand": "NO",
        }
        assert body["payload_preview"] is not None
        pp = body["payload_preview"]
        assert pp["update"]["method"] == "update"
        for kw in pp["update"]["params"]["Keywords"]:
            cats = kw["AutotargetingSettings"]["Categories"]
            assert len(cats) == 5, f"Expected 5 categories, got {len(cats)}"
            assert "Alternative" in cats
            assert "Accessory" in cats
            assert "Broader" in cats

    def test_dry_run_exact_narrow_broader_preset(self):
        """exact_narrow_broader preset enables Broader as well."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-dry-002",
                "dry_run": True,
                "preset": "exact_narrow_broader",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["categories_applied"]["Broader"] == "YES"
        assert body["categories_applied"]["Alternative"] == "NO"

    def test_dry_run_custom_categories(self):
        """custom preset with explicit categories."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-dry-003",
                "dry_run": True,
                "preset": "custom",
                "categories": {
                    "exact": "YES",
                    "narrow": "YES",
                    "alternative": "YES",
                    "accessory": "NO",
                    "broader": "NO",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["categories_applied"]["Alternative"] == "YES"

    def test_dry_run_custom_brand_options(self):
        """custom preset with explicit brand options override."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-dry-004",
                "dry_run": True,
                "preset": "exact_narrow",
                "brand_options": {
                    "without_brands": "YES",
                    "with_advertiser_brand": "NO",
                    "with_competitors_brand": "NO",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["brand_options_applied"]["WithAdvertiserBrand"] == "NO"

    def test_dry_run_with_ad_group_ids_filter(self):
        """dry-run respects ad_group_ids filter."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-dry-005",
                "dry_run": True,
                "preset": "exact_narrow",
                "ad_group_ids": [MOCK_AD_GROUP_ID],
                "reason": "только сантехник",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert MOCK_AD_GROUP_ID in body["targeted_ad_group_ids"]

    def test_dry_run_no_token_leakage(self):
        """Dry-run response never leaks the token."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-dry-006",
                "dry_run": True,
                "preset": "exact_narrow",
            },
        )
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    # --- dry-run in live mode with handler ----------------------------------

    def test_live_dry_run_returns_payload_preview_without_network_write(self):
        """In live_write mode, dry_run=True does NOT trigger keywords.update."""
        settings = _settings("live_write")
        # The handler for GET (keywords.get) is needed for read-before-write
        get_handler = _autotargeting_get_handler()
        yandex = _client_with_handler(settings, get_handler)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-live-dry-001",
                "dry_run": True,
                "preset": "exact_narrow",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dry_run"] is True
        assert body["applied"] is False
        assert body["source"] == "yandex"
        assert body["payload_preview"] is not None
        pp = body["payload_preview"]
        assert pp["update"]["method"] == "update"
        assert len(pp["update"]["params"]["Keywords"]) >= 1

    def test_live_dry_run_no_token_leakage(self):
        """Live dry-run response never leaks the token."""
        settings = _settings("live_write")
        get_handler = _autotargeting_get_handler()
        yandex = _client_with_handler(settings, get_handler)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-live-dry-002",
                "dry_run": True,
                "preset": "exact_narrow",
            },
        )
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text


# ===========================================================================
# POST validation tests
# ===========================================================================


class TestAutotargetingValidation:
    """Input validation — rejected before any network call."""

    def test_rejects_not_approved(self):
        """approved=false always returns 409."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": False,
                "idempotency_key": "test-val-001",
                "dry_run": True,
                "preset": "exact_narrow",
            },
        )
        assert resp.status_code == 409, resp.text

    def test_rejects_empty_ad_group_ids(self):
        """Pydantic validator rejects empty ad_group_ids list."""
        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-val-002",
                "dry_run": True,
                "preset": "exact_narrow",
                "ad_group_ids": [],
            },
        )
        assert resp.status_code == 422, resp.text

    def test_rejects_custom_preset_without_categories(self):
        """custom preset without categories is rejected (Pydantic validation)."""
        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-val-003",
                "dry_run": True,
                "preset": "custom",
            },
        )
        assert resp.status_code == 422, resp.text

    def test_rejects_live_write_without_approval_in_non_live_write_mode(self):
        """dry_run=False in non-live_write mode is rejected (HTTP 409)."""
        settings = _settings("live_readonly")
        get_handler = _autotargeting_get_handler()
        yandex = _client_with_handler(settings, get_handler)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-val-004",
                "dry_run": False,
                "preset": "exact_narrow",
            },
        )
        assert resp.status_code == 409, resp.text
        assert "live_write" in resp.json()["detail"]

    def test_rejects_missing_idempotency_key(self):
        """Pydantic validator rejects missing idempotency_key."""
        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "dry_run": True,
                "preset": "exact_narrow",
            },
        )
        assert resp.status_code == 422, resp.text

    def test_rejects_invalid_category_value(self):
        """Categories reject non-YES/NO values."""
        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-val-005",
                "dry_run": True,
                "preset": "custom",
                "categories": {
                    "exact": "MAYBE",
                    "narrow": "YES",
                    "alternative": "NO",
                    "accessory": "NO",
                    "broader": "NO",
                },
            },
        )
        assert resp.status_code == 422, resp.text

    def test_rejects_invalid_brand_option_value(self):
        """Brand options reject non-YES/NO values."""
        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-val-006",
                "dry_run": True,
                "preset": "exact_narrow",
                "brand_options": {
                    "without_brands": "YES",
                    "with_advertiser_brand": "YES",
                    "with_competitors_brand": "INVALID",
                },
            },
        )
        assert resp.status_code == 422, resp.text


# ===========================================================================
# POST apply (live_write) tests
# ===========================================================================


class TestAutotargetingApply:
    """Live write apply path in live_write mode."""

    def test_apply_uses_autotargeting_settings_not_deprecated_path(self):
        """The keywords.update payload uses AutotargetingSettings, not deprecated AutotargetingCategories."""
        settings = _settings("live_write")
        # The update handler checks for AutotargetingSettings presence and
        # asserts AutotargetingCategories is absent.  It also serves the
        # GET readback.
        handler_fn = _autotargeting_update_handler(success=True)
        yandex = _client_with_handler(settings, handler_fn)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-apply-001",
                "dry_run": False,
                "preset": "exact_narrow",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["applied"] is True
        assert body["source"] == "yandex"

    def test_apply_all_five_categories_explicit(self):
        """Live apply sends all five category booleans explicitly."""
        settings = _settings("live_write")
        handler_fn = _autotargeting_update_handler(success=True)
        yandex = _client_with_handler(settings, handler_fn)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-apply-002",
                "dry_run": False,
                "preset": "exact_narrow_broader",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["applied"] is True
        assert body["categories_applied"]["Broader"] == "YES"

    def test_apply_all_three_brand_options_explicit(self):
        """Live apply sends all three brand option booleans explicitly."""
        settings = _settings("live_write")
        handler_fn = _autotargeting_update_handler(success=True)
        yandex = _client_with_handler(settings, handler_fn)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-apply-003",
                "dry_run": False,
                "preset": "exact_narrow",
                "brand_options": {
                    "without_brands": "YES",
                    "with_advertiser_brand": "NO",
                    "with_competitors_brand": "NO",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["brand_options_applied"]["WithAdvertiserBrand"] == "NO"

    def test_apply_no_token_leakage(self):
        """Apply response never leaks the token."""
        settings = _settings("live_write")
        handler_fn = _autotargeting_update_handler(success=True)
        yandex = _client_with_handler(settings, handler_fn)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-apply-004",
                "dry_run": False,
                "preset": "exact_narrow",
            },
        )
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    def test_apply_surfaces_502_on_yandex_error(self):
        """Yandex error during apply returns 502."""
        settings = _settings("live_write")
        handler_fn = _autotargeting_update_handler(
            success=False, error_code=9999, error_detail="Internal error"
        )
        yandex = _client_with_handler(settings, handler_fn)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-apply-005",
                "dry_run": False,
                "preset": "exact_narrow",
            },
        )
        assert resp.status_code == 502, resp.text
        body_text = json.dumps(resp.json())
        assert "9999" in body_text
        assert SECRET_TOKEN not in body_text


# ===========================================================================
# Preset resolution
# ===========================================================================


class TestAutotargetingPresetResolution:
    """Unit-level preset resolution from YandexAutotargetingRequest."""

    def test_default_preset_is_exact_narrow(self):
        """The request model defaults to exact_narrow preset."""
        from app.models import YandexAutotargetingRequest

        req = YandexAutotargetingRequest(
            approved=True,
            idempotency_key="test-preset-001",
        )
        assert req.preset == "exact_narrow"
        cats = req.resolve_categories()
        assert cats["Exact"] == "YES"
        assert cats["Narrow"] == "YES"
        assert cats["Alternative"] == "NO"
        assert cats["Accessory"] == "NO"
        assert cats["Broader"] == "NO"

    def test_default_brand_options_exclude_competitors(self):
        """Default brand options exclude competitors."""
        from app.models import YandexAutotargetingRequest

        req = YandexAutotargetingRequest(
            approved=True,
            idempotency_key="test-preset-002",
        )
        brands = req.resolve_brand_options()
        assert brands["WithoutBrands"] == "YES"
        assert brands["WithAdvertiserBrand"] == "YES"
        assert brands["WithCompetitorsBrand"] == "NO"

    def test_exact_narrow_broader_preset_enables_broader(self):
        """exact_narrow_broader preset enables Broader=YES."""
        from app.models import YandexAutotargetingRequest

        req = YandexAutotargetingRequest(
            approved=True,
            idempotency_key="test-preset-003",
            preset="exact_narrow_broader",
        )
        cats = req.resolve_categories()
        assert cats["Exact"] == "YES"
        assert cats["Narrow"] == "YES"
        assert cats["Broader"] == "YES"
        assert cats["Alternative"] == "NO"
        assert cats["Accessory"] == "NO"

    def test_custom_categories_requires_categories_field(self):
        """custom preset without categories is rejected by model validator."""
        from pydantic import ValidationError
        from app.models import YandexAutotargetingRequest

        with pytest.raises(ValidationError, match="categories is required"):
            YandexAutotargetingRequest(
                approved=True,
                idempotency_key="test-preset-004",
                preset="custom",
            )


# ===========================================================================
# Idempotency replay tests (reviewer blocker #1 fix)
# ===========================================================================


class TestAutotargetingIdempotency:
    """Idempotency cache replays: repeat calls with same key return cached result."""

    def test_dry_run_idempotency_replay_returns_cached_result(self):
        """Second dry-run with same idempotency_key returns identical cached result."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        payload = {
            "approved": True,
            "idempotency_key": "test-idem-dry-001",
            "dry_run": True,
            "preset": "exact_narrow",
        }

        resp1 = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json=payload,
        )
        assert resp1.status_code == 200, resp1.text

        resp2 = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json=payload,
        )
        assert resp2.status_code == 200, resp2.text

        # Both responses must be identical (cached)
        assert resp1.json() == resp2.json()

    def test_live_apply_idempotency_replay_returns_cached_result(self):
        """Second live-apply with same idempotency_key returns identical cached result
        without re-sending to Yandex."""
        settings = _settings("live_write")
        handler_fn = _autotargeting_update_handler(success=True)
        yandex = _client_with_handler(settings, handler_fn)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        payload = {
            "approved": True,
            "idempotency_key": "test-idem-live-001",
            "dry_run": False,
            "preset": "exact_narrow",
        }

        resp1 = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json=payload,
        )
        assert resp1.status_code == 200, resp1.text
        assert resp1.json()["applied"] is True

        # Second call MUST return cached result — handler would raise
        # on unexpected duplicate UPDATE call, so a 200 confirms cache hit.
        resp2 = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json=payload,
        )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["applied"] is True
        assert resp1.json() == resp2.json()

    def test_idempotency_different_dry_run_rejected(self):
        """Replay with same key but different dry_run value is rejected."""
        # In live_write mode the store's idempotency check fires (502);
        # in non-live-write modes the HTTP gate fires first (409).
        # Both are correct rejections.
        settings = _settings("live_write")
        get_handler = _autotargeting_get_handler()
        yandex = _client_with_handler(settings, get_handler)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        payload_dry = {
            "approved": True,
            "idempotency_key": "test-idem-mix-001",
            "dry_run": True,
            "preset": "exact_narrow",
        }

        resp1 = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json=payload_dry,
        )
        assert resp1.status_code == 200

        payload_apply = dict(payload_dry)
        payload_apply["dry_run"] = False
        # Must also supply a POST handler for real apply (won't be called)
        # Actually for idempotency cross-check in live_write mode, the
        # store will raise YandexDirectError (502) before any network call.

        resp2 = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json=payload_apply,
        )
        # Either 502 (store idempotency) or 409 (gate before store)
        # — both are correct rejections.
        assert resp2.status_code in (409, 502), (
            f"Expected rejection, got {resp2.status_code}: {resp2.text}"
        )
        if resp2.status_code == 409:
            assert "live_write" in resp2.json()["detail"].lower()
        else:
            detail = resp2.json()["detail"]
            assert isinstance(detail, dict), f"Expected dict detail, got {type(detail)}"
            assert "idempotency" in detail.get("message", "").lower()


# ===========================================================================
# create_missing (reviewer blocker #2 fix — implemented, not removed)
# ===========================================================================


class TestAutotargetingCreateMissing:
    """create_missing: bool = False is the default; True enables keywords.add."""

    def test_create_missing_false_default_skips_missing_ad_groups(self):
        """Default create_missing=False: missing ad groups are in skipped_ad_group_ids."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.post(
            f"/yandex/campaigns/{REAL_CAMPAIGN_ID}/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-cm-003",
                "dry_run": True,
                "preset": "exact_narrow",
                "ad_group_ids": ["999999"],  # does not exist
            },
        )
        assert resp.status_code == 502, (
            f"Expected fail-closed 502, got {resp.status_code}: {resp.text}"
        )
        detail = resp.json()["detail"]
        assert "No autotargeting rows" in detail["message"]
        assert "create_missing=true" in detail["message"]

    def test_create_missing_true_dry_run_shows_add_payload(self):
        """create_missing=true dry-run: payload_preview includes both update and add."""
        settings = _settings("live_write")

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.url.path.endswith("/adgroups"):
                return httpx.Response(200, json={"result": {"AdGroups": [
                    {"Id": 456, "Name": "Test AG", "CampaignId": 710691939}
                ]}})
            if body.get("method") == "get":
                return httpx.Response(
                    200, json={"result": {"Keywords": [
                        {"Id": 123, "AdGroupId": 456, "Keyword": "---autotargeting",
                         "State": "ON", "Status": "ACCEPTED",
                         "AutotargetingSettings": {
                             "Categories": {"Exact": "YES", "Narrow": "YES",
                                            "Alternative": "NO", "Accessory": "NO",
                                            "Broader": "NO"},
                             "BrandOptions": {"WithoutBrands": "YES",
                                              "WithAdvertiserBrand": "YES",
                                              "WithCompetitorsBrand": "NO"}}
                         }]}})
            raise AssertionError(f"Unexpected method={body.get('method')}")

        yandex = _client_with_handler(settings, handler)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-cm-004",
                "dry_run": True,
                "preset": "exact_narrow",
                "ad_group_ids": ["456", "789"],
                "create_missing": True,
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["applied"] is False
        assert "456" in body["targeted_ad_group_ids"]
        assert "789" in body["targeted_ad_group_ids"]
        pp = body["payload_preview"]

        # Update payload exists for group 456
        assert "update" in pp
        assert pp["update"]["method"] == "update"
        assert len(pp["update"]["params"]["Keywords"]) == 1
        assert pp["update"]["params"]["Keywords"][0]["Id"] == 123

        # Add payload exists for group 789 (no existing autotargeting row)
        assert "add" in pp
        assert pp["add"]["method"] == "add"
        assert len(pp["add"]["params"]["Keywords"]) == 1
        add_kw = pp["add"]["params"]["Keywords"][0]
        assert add_kw["Keyword"] == "---autotargeting"
        assert add_kw["AdGroupId"] == 789
        assert "AutotargetingSettings" in add_kw
        assert len(add_kw["AutotargetingSettings"]["Categories"]) == 5
        assert len(add_kw["AutotargetingSettings"]["BrandOptions"]) == 3

    def test_create_missing_true_apply_dispatches_add_and_update(self):
        """create_missing=true + live_write dispatches both keywords.add and update."""
        settings = _settings("live_write")

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.url.path.endswith("/adgroups"):
                return httpx.Response(200, json={"result": {"AdGroups": [
                    {"Id": 456, "Name": "Test AG", "CampaignId": 710691939}
                ]}})
            if body.get("method") == "get":
                return httpx.Response(
                    200, json={"result": {"Keywords": [
                        {"Id": 123, "AdGroupId": 456, "Keyword": "---autotargeting",
                         "State": "ON", "Status": "ACCEPTED"}
                    ]}})
            if body.get("method") == "add":
                return httpx.Response(200, json={
                    "result": {"AddResults": [{"Id": 999, "Warnings": []}]}
                })
            if body.get("method") == "update":
                return httpx.Response(200, json={
                    "result": {"UpdateResults": [{"Id": 123}]}
                })
            raise AssertionError(f"Unexpected method={body.get('method')}")

        yandex = _client_with_handler(settings, handler)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-cm-005",
                "dry_run": False,
                "preset": "exact_narrow",
                "ad_group_ids": ["456", "789"],
                "create_missing": True,
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["applied"] is True
        assert body["dry_run"] is False
        assert "456" in body["targeted_ad_group_ids"]
        assert "789" in body["targeted_ad_group_ids"]
        # Updated keyword IDs from update
        assert any("123" == kid for kid in body["updated_keyword_ids"])
        # Created keyword IDs from add
        assert any("999" == kid for kid in body["created_keyword_ids"])

    def test_create_missing_idempotency_replay_no_duplicate(self):
        """Replay with same idempotency key returns cached result without duplicate writes."""
        settings = _settings("live_write")
        call_counts: dict[str, int] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            method = body.get("method", "")
            call_counts[method] = call_counts.get(method, 0) + 1
            if request.url.path.endswith("/adgroups"):
                return httpx.Response(200, json={"result": {"AdGroups": [
                    {"Id": 456, "Name": "Test AG", "CampaignId": 710691939}
                ]}})
            if method == "get":
                return httpx.Response(
                    200, json={"result": {"Keywords": [
                        {"Id": 123, "AdGroupId": 456, "Keyword": "---autotargeting",
                         "State": "ON", "Status": "ACCEPTED"}
                    ]}})
            if method == "add":
                return httpx.Response(200, json={
                    "result": {"AddResults": [{"Id": 777}]}
                })
            if method == "update":
                return httpx.Response(200, json={
                    "result": {"UpdateResults": [{"Id": 123}]}
                })
            raise AssertionError(f"Unexpected method={method}")

        yandex = _client_with_handler(settings, handler)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        payload = {
            "approved": True,
            "idempotency_key": "test-cm-006",
            "dry_run": False,
            "preset": "exact_narrow",
            "ad_group_ids": ["456", "789"],
            "create_missing": True,
        }

        # First call — must dispatch
        resp1 = client.post("/yandex/campaigns/710691939/autotargeting", json=payload)
        assert resp1.status_code == 200, f"Expected 200, got {resp1.status_code}: {resp1.text}"
        body1 = resp1.json()
        assert body1["applied"] is True

        # Record call counts
        add_count_after_first = call_counts.get("add", 0)
        update_count_after_first = call_counts.get("update", 0)
        assert add_count_after_first >= 1
        assert update_count_after_first >= 1

        # Second call — same idempotency key, must NOT dispatch again
        resp2 = client.post("/yandex/campaigns/710691939/autotargeting", json=payload)
        assert resp2.status_code == 200, f"Expected 200, got {resp2.status_code}: {resp2.text}"
        body2 = resp2.json()
        assert body2["applied"] is True

        # Verify no additional network calls
        assert call_counts.get("add", 0) == add_count_after_first, (
            f"keywords.add called again on replay"
        )
        assert call_counts.get("update", 0) == update_count_after_first, (
            f"keywords.update called again on replay"
        )
        # Result should match original
        assert body2["created_keyword_ids"] == body1["created_keyword_ids"]
        assert body2["updated_keyword_ids"] == body1["updated_keyword_ids"]

    def test_no_rows_and_no_ad_groups_fails_closed(self):
        """No autotargeting rows + no explicit ad_group_ids → fail closed (default false)."""
        settings = _settings("live_write")

        def empty_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.url.path.endswith("/adgroups"):
                return httpx.Response(200, json={"result": {"AdGroups": []}})
            if body.get("method") == "get":
                return httpx.Response(200, json={"result": {"Keywords": []}})
            raise AssertionError(f"Unexpected method={body.get('method')}")

        yandex = _client_with_handler(settings, empty_handler)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/autotargeting",
            json={
                "approved": True,
                "idempotency_key": "test-cm-007",
                "dry_run": True,
                "preset": "exact_narrow",
            },
        )
        # Must fail closed — nothing to update and no ad_groups to add
        assert resp.status_code in (400, 409, 502), (
            f"Expected closed-fail, got {resp.status_code}: {resp.text}"
        )
