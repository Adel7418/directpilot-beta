"""Tests for UTM integration in campaign creation (drafts + live-create).

Covers:
- Campaign draft creation with utm_config stores it on the draft
- Campaign draft preview shows UTM-augmented Hrefs when utm enabled
- Live-create dry-run with utm enabled → TextAd.Href has UTM params
- Live-create dry-run with utm disabled → keeps existing href unchanged
- Fragment/query preserved when UTM applied
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _create_draft(
    business_type: str = "remont",
    region: str = "Казань",
    monthly_budget: float = 45000,
    landing_url: str = "https://example.ru/remont",
    utm_config: dict | None = None,
) -> str:
    payload: dict[str, Any] = {
        "business_type": business_type,
        "region": region,
        "monthly_budget": monthly_budget,
        "landing_url": landing_url,
    }
    if utm_config is not None:
        payload["utm_config"] = utm_config
    response = client.post("/campaign-drafts", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _add_ad_group(draft_id: str, name: str = "Test Group", keywords: list[str] | None = None) -> str:
    if keywords is None:
        keywords = ["test keyword"]
    resp = client.post(
        f"/campaign-drafts/{draft_id}/ad-groups",
        json={"name": name, "keywords": keywords},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["ad_groups"][-1]["id"]


def _add_ad(
    draft_id: str,
    ad_group_id: str,
    title: str = "Test Ad",
    text: str = "Buy now",
    landing_url: str = "https://example.ru/remont?ref=dp#offer",
) -> str:
    resp = client.post(
        f"/campaign-drafts/{draft_id}/ads",
        json={
            "ad_group_id": ad_group_id,
            "title": title,
            "text": text,
            "landing_url": landing_url,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["ads"][-1]["id"]


def _live_create_payload(
    draft_id: str,
    *,
    approved: bool = True,
    dry_run: bool = True,
    utm_config: dict | None = None,
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "draft_id": draft_id,
        "approved": approved,
        "idempotency_key": f"test-lc-{draft_id}",
        "dry_run": dry_run,
    }
    if utm_config is not None:
        p["utm_config"] = utm_config
    return p


# ---------------------------------------------------------------------------
# UTM config stored on draft
# ---------------------------------------------------------------------------


class TestUtmInCampaignDraft:
    def test_draft_stores_utm_config_when_provided(self):
        """Campaign draft creation with utm_config stores it on the draft."""
        utm_cfg = {"enabled": True, "campaign_slug": "my-slug", "overwrite": False}
        draft_id = _create_draft(utm_config=utm_cfg)
        draft = store.drafts[draft_id]
        assert draft.utm_config is not None
        assert draft.utm_config.enabled is True
        assert draft.utm_config.campaign_slug == "my-slug"
        assert draft.utm_config.overwrite is False

    def test_draft_utm_config_defaults_to_none(self):
        """Without utm_config, draft stores None."""
        draft_id = _create_draft()
        draft = store.drafts[draft_id]
        assert draft.utm_config is None

    def test_draft_preview_shows_utm_augmented_hrefs_when_enabled(self):
        """Preview payload shows UTM-tagged Hrefs when utm_config.enabled=True."""
        utm_cfg = {"enabled": True, "campaign_slug": "test-slug"}
        draft_id = _create_draft(
            landing_url="https://example.ru/remont?ref=dp#offer",
            utm_config=utm_cfg,
        )
        gid = _add_ad_group(draft_id)
        _add_ad(draft_id, gid, landing_url="https://example.ru/remont?ref=dp#offer")

        resp = client.get(f"/campaign-drafts/{draft_id}/preview")
        assert resp.status_code == 200
        body = resp.json()

        # Check ads in yandex_payload have UTM
        yp = body.get("yandex_payload", {})
        ads = yp.get("ads", [])
        assert len(ads) > 0
        ad_href = ads[0].get("landing_url", "")
        # The preview should have UTM params injected
        assert "utm_source=yandex" in ad_href
        assert "utm_medium=cpc" in ad_href
        assert "utm_campaign=test-slug" in ad_href
        # Fragment preserved
        assert "#offer" in ad_href

    def test_draft_preview_keeps_raw_href_when_utm_disabled(self):
        """Preview keeps raw Href when utm_config is absent or enabled=False."""
        draft_id = _create_draft(
            landing_url="https://example.ru/remont?ref=dp#offer",
        )
        gid = _add_ad_group(draft_id)
        _add_ad(draft_id, gid, landing_url="https://example.ru/remont?ref=dp#offer")

        resp = client.get(f"/campaign-drafts/{draft_id}/preview")
        assert resp.status_code == 200
        body = resp.json()
        yp = body.get("yandex_payload", {})
        ads = yp.get("ads", [])
        assert len(ads) > 0
        ad_href = ads[0].get("landing_url", "")
        # No UTM params
        assert "utm_source" not in ad_href


# ---------------------------------------------------------------------------
# Live-create dry-run with UTM
# ---------------------------------------------------------------------------


class TestUtmInLiveCreate:
    def test_live_create_dry_run_adds_utm_to_textad_href_when_enabled(self):
        """dry_run=True with utm_config.enabled=True → TextAd.Href has UTM."""
        utm_cfg = {"enabled": True, "campaign_slug": "lc-slug"}
        draft_id = _create_draft(
            landing_url="https://example.ru/remont?ref=dp#offer",
            utm_config=utm_cfg,
        )
        gid = _add_ad_group(draft_id)
        _add_ad(draft_id, gid, landing_url="https://example.ru/remont?ref=dp#offer")

        resp = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(draft_id, utm_config=utm_cfg),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dry_run"] is True
        assert body["applied"] is False

        # Inspect the ads.add stage in the chain preview
        pp = body.get("payload_preview", {})
        chain = pp.get("params", {}).get("chain", [])
        # Stage 2 (index 1) is ads.add
        ads_stage = chain[1] if len(chain) > 1 else {}
        ads_list = ads_stage.get("params", {}).get("Ads", [])
        assert len(ads_list) > 0, f"No ads in chain preview: {json.dumps(chain, indent=2)}"

        for ad_item in ads_list:
            ta = ad_item.get("TextAd", {})
            href = ta.get("Href", "")
            assert "utm_source=yandex" in href, f"Missing utm_source in: {href}"
            assert "utm_medium=cpc" in href
            assert "utm_campaign=lc-slug" in href
            # Preserve query
            assert "ref=dp" in href
            # Preserve fragment
            assert "#offer" in href

    def test_live_create_dry_run_keeps_raw_href_when_utm_disabled(self):
        """dry_run=True without utm_config → TextAd.Href unchanged."""
        draft_id = _create_draft(
            landing_url="https://example.ru/remont?ref=dp#offer",
        )
        gid = _add_ad_group(draft_id)
        _add_ad(draft_id, gid, landing_url="https://example.ru/remont?ref=dp#offer")

        resp = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(draft_id),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        pp = body.get("payload_preview", {})
        chain = pp.get("params", {}).get("chain", [])
        ads_stage = chain[1] if len(chain) > 1 else {}
        ads_list = ads_stage.get("params", {}).get("Ads", [])
        assert len(ads_list) > 0

        for ad_item in ads_list:
            ta = ad_item.get("TextAd", {})
            href = ta.get("Href", "")
            # No UTM params
            assert "utm_source" not in href
            assert "utm_medium" not in href

    def test_live_create_utm_preserves_fragment(self):
        """Fragment (#anchor) is preserved after UTM injection."""
        utm_cfg = {"enabled": True, "campaign_slug": "frag-test"}
        draft_id = _create_draft(
            landing_url="https://example.ru/page?sort=asc#section2",
            utm_config=utm_cfg,
        )
        gid = _add_ad_group(draft_id)
        _add_ad(draft_id, gid, landing_url="https://example.ru/page?sort=asc#section2")

        resp = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(draft_id, utm_config=utm_cfg),
        )
        assert resp.status_code == 200
        body = resp.json()
        pp = body.get("payload_preview", {})
        chain = pp.get("params", {}).get("chain", [])
        ads_stage = chain[1] if len(chain) > 1 else {}
        ads_list = ads_stage.get("params", {}).get("Ads", [])
        assert len(ads_list) > 0
        href = ads_list[0]["TextAd"]["Href"]
        assert href.endswith("#section2")
        assert "sort=asc" in href

    def test_live_create_utm_overwrite_false_preserves_existing(self):
        """When overwrite=False, existing UTM params on the URL are kept."""
        utm_cfg = {"enabled": True, "campaign_slug": "ow-test", "overwrite": False}
        draft_id = _create_draft(
            landing_url="https://example.ru/page?utm_source=google",
            utm_config=utm_cfg,
        )
        gid = _add_ad_group(draft_id)
        _add_ad(draft_id, gid, landing_url="https://example.ru/page?utm_source=google")

        resp = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(draft_id, utm_config=utm_cfg),
        )
        assert resp.status_code == 200
        body = resp.json()
        pp = body.get("payload_preview", {})
        chain = pp.get("params", {}).get("chain", [])
        ads_stage = chain[1] if len(chain) > 1 else {}
        ads_list = ads_stage.get("params", {}).get("Ads", [])
        href = ads_list[0]["TextAd"]["Href"]
        # Existing UTM preserved — new UTM not applied
        assert "utm_source=google" in href

    def test_live_create_utm_overwrite_true_replaces_existing(self):
        """When overwrite=True, existing UTM is replaced."""
        utm_cfg = {"enabled": True, "campaign_slug": "ow-replace", "overwrite": True}
        draft_id = _create_draft(
            landing_url="https://example.ru/page?utm_source=google",
            utm_config=utm_cfg,
        )
        gid = _add_ad_group(draft_id)
        _add_ad(draft_id, gid, landing_url="https://example.ru/page?utm_source=google")

        resp = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(draft_id, utm_config=utm_cfg),
        )
        assert resp.status_code == 200
        body = resp.json()
        pp = body.get("payload_preview", {})
        chain = pp.get("params", {}).get("chain", [])
        ads_stage = chain[1] if len(chain) > 1 else {}
        ads_list = ads_stage.get("params", {}).get("Ads", [])
        href = ads_list[0]["TextAd"]["Href"]
        assert "utm_source=yandex" in href
        assert "utm_source=google" not in href

    def test_live_create_utm_config_override_draft_when_draft_has_no_utm(self):
        """When draft has no utm_config, LiveCreateCampaignRequest.utm_config
        MUST supply UTM tags to ad Hrefs."""
        # Draft without UTM
        draft_id = _create_draft(
            landing_url="https://example.ru/page",
        )
        gid = _add_ad_group(draft_id)
        _add_ad(draft_id, gid, landing_url="https://example.ru/page")

        # Live-create with UTM override (draft has None)
        override_utm = {"enabled": True, "campaign_slug": "override-slug"}
        resp = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(draft_id, utm_config=override_utm),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        pp = body.get("payload_preview", {})
        chain = pp.get("params", {}).get("chain", [])
        ads_stage = chain[1] if len(chain) > 1 else {}
        ads_list = ads_stage.get("params", {}).get("Ads", [])
        assert len(ads_list) > 0
        href = ads_list[0]["TextAd"]["Href"]
        assert "utm_source=yandex" in href
        assert "utm_campaign=override-slug" in href

    def test_live_create_utm_config_override_wins_over_draft_utm(self):
        """When both draft.utm_config and LiveCreateCampaignRequest.utm_config
        are set, the request's override MUST take precedence."""
        # Draft with UTM slug "draft-slug"
        draft_utm = {"enabled": True, "campaign_slug": "draft-slug"}
        draft_id = _create_draft(
            landing_url="https://example.ru/page",
            utm_config=draft_utm,
        )
        gid = _add_ad_group(draft_id)
        _add_ad(draft_id, gid, landing_url="https://example.ru/page")

        # Live-create with a DIFFERENT slug as override
        override_utm = {"enabled": True, "campaign_slug": "override-slug"}
        resp = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(draft_id, utm_config=override_utm),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        pp = body.get("payload_preview", {})
        chain = pp.get("params", {}).get("chain", [])
        ads_stage = chain[1] if len(chain) > 1 else {}
        ads_list = ads_stage.get("params", {}).get("Ads", [])
        assert len(ads_list) > 0
        href = ads_list[0]["TextAd"]["Href"]
        assert "utm_campaign=override-slug" in href, (
            f"Expected override-slug to win, got: {href}"
        )
        assert "utm_campaign=draft-slug" not in href, (
            f"Draft slug must NOT appear when override is set, got: {href}"
        )

    def test_live_create_utm_config_override_disabled_suppresses_utm(self):
        """When draft has UTM enabled but request passes utm_config with
        enabled=False, the override disables UTM tagging."""
        draft_utm = {"enabled": True, "campaign_slug": "draft-slug"}
        draft_id = _create_draft(
            landing_url="https://example.ru/page",
            utm_config=draft_utm,
        )
        gid = _add_ad_group(draft_id)
        _add_ad(draft_id, gid, landing_url="https://example.ru/page")

        # Override with enabled=False — should suppress UTM
        override_utm = {"enabled": False, "campaign_slug": "no-utm"}
        resp = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(draft_id, utm_config=override_utm),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        pp = body.get("payload_preview", {})
        chain = pp.get("params", {}).get("chain", [])
        ads_stage = chain[1] if len(chain) > 1 else {}
        ads_list = ads_stage.get("params", {}).get("Ads", [])
        assert len(ads_list) > 0
        href = ads_list[0]["TextAd"]["Href"]
        assert "utm_source" not in href, (
            f"UTM must be suppressed when override has enabled=False, got: {href}"
        )
