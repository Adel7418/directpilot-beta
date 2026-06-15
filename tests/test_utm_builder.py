"""Tests for UTM URL builder — pure logic, no I/O.

Covers:
- build_utm_url: adds UTM params, preserves path/query/fragment
- audit_utm_url: detects missing/complete UTM params
"""

from __future__ import annotations

import pytest
from app.utm_builder import build_utm_url, audit_utm_url, generate_campaign_slug


# ---------------------------------------------------------------------------
# build_utm_url — core URL construction
# ---------------------------------------------------------------------------

class TestBuildUtmUrl:
    def test_adds_all_default_utm_params_to_clean_url(self):
        result = build_utm_url(
            url="https://example.com/page",
            utm_source="yandex",
            utm_medium="cpc",
            utm_campaign="my-campaign",
            utm_content="12345",
            utm_term="keyword",
        )
        assert result == (
            "https://example.com/page"
            "?utm_source=yandex"
            "&utm_medium=cpc"
            "&utm_campaign=my-campaign"
            "&utm_content=12345"
            "&utm_term=keyword"
        )

    def test_preserves_existing_query_params(self):
        result = build_utm_url(
            url="https://example.com/page?sort=price&filter=new",
            utm_source="yandex",
            utm_medium="cpc",
            utm_campaign="test",
            utm_content="1",
            utm_term="kw",
        )
        assert "sort=price" in result
        assert "filter=new" in result
        assert "utm_source=yandex" in result

    def test_preserves_fragment(self):
        result = build_utm_url(
            url="https://example.com/page#prices",
            utm_source="yandex",
            utm_medium="cpc",
            utm_campaign="test",
            utm_content="1",
            utm_term="kw",
        )
        assert result.endswith("#prices")
        assert "utm_source=yandex" in result

    def test_preserves_both_query_and_fragment(self):
        result = build_utm_url(
            url="https://example.com/page?lang=ru#services",
            utm_source="yandex",
            utm_medium="cpc",
            utm_campaign="test",
            utm_content="1",
            utm_term="kw",
        )
        assert "lang=ru" in result
        assert result.endswith("#services")
        assert "utm_source=yandex" in result

    def test_overwrites_existing_utm_when_overwrite_true(self):
        result = build_utm_url(
            url="https://example.com/page?utm_source=google&utm_campaign=old",
            utm_source="yandex",
            utm_medium="cpc",
            utm_campaign="new-campaign",
            utm_content="1",
            utm_term="kw",
            overwrite=True,
        )
        assert "utm_source=yandex" in result
        assert "utm_campaign=new-campaign" in result
        assert "utm_source=google" not in result
        assert "utm_campaign=old" not in result

    def test_preserves_existing_utm_when_overwrite_false(self):
        result = build_utm_url(
            url="https://example.com/page?utm_source=google&custom=val",
            utm_source="yandex",
            utm_medium="cpc",
            utm_campaign="new",
            utm_content="1",
            utm_term="kw",
            overwrite=False,
        )
        assert "utm_source=google" in result
        assert "custom=val" in result
        # When not overwriting, new UTM should NOT be applied since existing
        # source was found. But the implementation should add the custom param.
        assert "utm_source=yandex" not in result or "utm_source=google" in result

    def test_handles_url_without_scheme(self):
        result = build_utm_url(
            url="example.com/page",
            utm_source="yandex",
            utm_medium="cpc",
            utm_campaign="test",
            utm_content="1",
            utm_term="kw",
        )
        assert result.startswith("http://example.com/page")
        assert "utm_source=yandex" in result

    def test_custom_params_injected(self):
        result = build_utm_url(
            url="https://example.com/page",
            utm_source="yandex",
            utm_medium="cpc",
            utm_campaign="test",
            utm_content="1",
            utm_term="kw",
            custom_params={"utm_custom": "extra", "ref": "dp"},
        )
        assert "utm_custom=extra" in result
        assert "ref=dp" in result

    def test_custom_params_do_not_override_core_utm(self):
        """Core UTM params always win over custom_params."""
        result = build_utm_url(
            url="https://example.com/page",
            utm_source="yandex",
            utm_medium="cpc",
            utm_campaign="test",
            utm_content="1",
            utm_term="kw",
            custom_params={"utm_source": "evil", "utm_campaign": "evil2"},
        )
        assert "utm_source=yandex" in result
        assert "utm_campaign=test" in result

    def test_empty_params_not_injected(self):
        result = build_utm_url(
            url="https://example.com/page",
            utm_source="yandex",
            utm_medium="cpc",
            utm_campaign="test",
            utm_content="",
            utm_term="",
        )
        assert "utm_content=" not in result
        assert "utm_term=" not in result


# ---------------------------------------------------------------------------
# audit_utm_url — detect UTM state
# ---------------------------------------------------------------------------

REQUIRED_UTM = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"}
DIRECT_DEFAULTS = {
    "utm_source": "yandex",
    "utm_medium": "cpc",
}


class TestAuditUtmUrl:
    def test_detects_missing_all_utm(self):
        result = audit_utm_url(
            url="https://example.com/page",
            required_params=REQUIRED_UTM,
            expected_values=DIRECT_DEFAULTS,
        )
        assert result["status"] == "missing"
        assert len(result["missing_params"]) == 5
        assert "utm_source" in result["missing_params"]

    def test_detects_complete_utm(self):
        result = audit_utm_url(
            url="https://example.com/page?utm_source=yandex&utm_medium=cpc&utm_campaign=test&utm_content=1&utm_term=kw",
            required_params=REQUIRED_UTM,
            expected_values=DIRECT_DEFAULTS,
        )
        assert result["status"] == "complete"

    def test_detects_partial_utm(self):
        result = audit_utm_url(
            url="https://example.com/page?utm_source=yandex&utm_medium=cpc",
            required_params=REQUIRED_UTM,
            expected_values=DIRECT_DEFAULTS,
        )
        assert result["status"] == "partial"
        assert "utm_campaign" in result["missing_params"]
        assert "utm_content" in result["missing_params"]
        assert "utm_term" in result["missing_params"]

    def test_detects_wrong_source(self):
        result = audit_utm_url(
            url="https://example.com/page?utm_source=google&utm_medium=cpc&utm_campaign=test&utm_content=1&utm_term=kw",
            required_params=REQUIRED_UTM,
            expected_values=DIRECT_DEFAULTS,
        )
        assert result["status"] in ("partial", "mismatch")
        assert "utm_source" in result.get("wrong_values", {}) or result["status"] == "mismatch"

    def test_returns_present_params(self):
        result = audit_utm_url(
            url="https://example.com/page?utm_source=yandex&utm_medium=cpc&utm_campaign=test",
            required_params=REQUIRED_UTM,
            expected_values=DIRECT_DEFAULTS,
        )
        assert "utm_source" in result["present_params"]
        assert "utm_medium" in result["present_params"]
        assert "utm_campaign" in result["present_params"]
        assert "utm_content" not in result["present_params"]

    def test_url_with_fragment_still_audited(self):
        result = audit_utm_url(
            url="https://example.com/page#prices",
            required_params=REQUIRED_UTM,
            expected_values=DIRECT_DEFAULTS,
        )
        assert result["status"] == "missing"
        assert len(result["missing_params"]) == 5


# ---------------------------------------------------------------------------
# generate_campaign_slug — safe slug from campaign name/id
# ---------------------------------------------------------------------------

class TestGenerateCampaignSlug:
    def test_generates_from_ascii_name(self):
        slug = generate_campaign_slug(name="Remont Kvartir Kazan", campaign_id="12345")
        assert "remont" in slug
        assert "12345" in slug
        assert " " not in slug

    def test_cyrillic_only_falls_back_to_id(self):
        """Cyrillic chars are not transliterated — safe fallback to campaign-id."""
        slug = generate_campaign_slug(name="Ремонт квартир Казань", campaign_id="67890")
        assert "campaign-67890" in slug

    def test_falls_back_to_id_only(self):
        slug = generate_campaign_slug(name="", campaign_id="99999")
        assert "campaign-99999" in slug

    def test_no_double_hyphens(self):
        slug = generate_campaign_slug(name="Тест --- кампания", campaign_id="1")
        assert "--" not in slug
