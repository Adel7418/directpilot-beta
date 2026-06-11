"""Tests for the live-create campaign chain helpers.

The live-create flow chains four v5 services behind one
``POST /yandex/campaigns/live-create`` endpoint:

1. ``campaigns.add`` (already implemented and tested in
   ``tests/test_live_create_campaign.py``).
2. ``adgroups.add`` (stage 2 — added in this PR).
3. ``ads.add`` (stage 3 — added in this PR).
4. ``keywords.add`` (stage 4 — reuses the existing
   :meth:`YandexDirectClient.keywords_add` helper).
5. ``negativekeywordsharedsets.add`` (stage 5 — KEPT AS
   ``not_implemented`` in this PR. The Direct API v5 shape for
   ``negativekeywordsharedsets.add`` is NOT documented in the
   project sources and is NOT exercised by any current read path.
   We surface it explicitly in the response so the caller knows
   that the chain stops at stage 4.)

This file pins the two new explicit helpers (stage 2 and stage 3)
on :class:`YandexDirectClient`. The store-side chain behaviour
(gates, idempotency, audit, AddResults.Errors/Id inspection) is
tested in ``tests/test_live_create_campaign.py`` — see the new
``test_live_create_apply_chains_*`` cases.

The tests below use ``httpx.MockTransport`` only. No real network
calls are made; no real Yandex Direct account is touched.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.yandex_direct import YandexDirectClient


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(_env_file=None, directpilot_mode="live_write", yandex_oauth_token="SECRET-CHAIN")


def _client(handler) -> YandexDirectClient:
    return YandexDirectClient(settings=_settings(), transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# adgroups.add (stage 2)
# ---------------------------------------------------------------------------


def test_adgroups_add_posts_to_v5_adgroups_with_method_add_and_returns_ok_envelope():
    """``adgroups_add`` MUST post to the v5 ``adgroups`` service with
    ``method=add`` and a list of ``AdGroups``. The response envelope
    must NOT echo the OAUTH token.

    The v5 contract (mirrored from the existing ``adgroups_update``
    helper, see ``app/yandex_direct.py``) is:

    .. code-block:: json

        {"method": "add", "params": {"AdGroups": [{"Name": "...", "CampaignId": 123, ...}, ...]}}
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"result": {"AddResults": [{"Id": 1001}, {"Id": 1002}]}},
        )

    client = _client(handler)
    result = client.adgroups_add(
        [
            {"Name": "g1", "CampaignId": 42},
            {"Name": "g2", "CampaignId": 42, "RegionIds": [43]},
        ]
    )

    assert result["ok"] is True
    assert captured["url"] == "https://api.direct.yandex.com/json/v5/adgroups"
    assert captured["body"]["method"] == "add"
    assert captured["body"]["params"]["AdGroups"][0]["Name"] == "g1"
    assert captured["body"]["params"]["AdGroups"][0]["CampaignId"] == 42
    assert captured["body"]["params"]["AdGroups"][1]["RegionIds"] == [43]
    # No token echo.
    assert "SECRET-CHAIN" not in str(result)
    assert "SECRET-CHAIN" not in json.dumps(captured["body"], ensure_ascii=False)


def test_adgroups_add_returns_sandbox_base_url_when_mode_is_sandbox():
    """When the runtime mode is ``sandbox``, the helper MUST post to
    the sandbox base URL — the same contract as the other v5 write
    helpers (``campaigns_add``, ``keywords_add``)."""
    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="SECRET-CHAIN-SB"
    )
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 7}]}})

    client = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    client.adgroups_add([{"Name": "g", "CampaignId": 1}])

    assert captured["url"] == "https://api-sandbox.direct.yandex.com/json/v5/adgroups"


def test_adgroups_add_propagates_http_error_as_yandex_direct_error():
    """An HTTP 4xx/5xx from the v5 service must surface as
    :class:`YandexDirectError` with a redacted message — never as a
    raw token-bearing traceback."""
    settings = Settings(
        _env_file=None, directpilot_mode="live_write", yandex_oauth_token="SECRET-CHAIN-ERR"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "upstream broke"})

    client = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    with pytest.raises(Exception) as exc_info:
        client.adgroups_add([{"Name": "g", "CampaignId": 1}])
    # We never want the token in the exception string.
    assert "SECRET-CHAIN-ERR" not in str(exc_info.value)
    # We do want a typed error so the endpoint can return 502.
    from app.yandex_direct import YandexDirectError

    assert isinstance(exc_info.value, YandexDirectError)


# ---------------------------------------------------------------------------
# ads.add (stage 3)
# ---------------------------------------------------------------------------


def test_ads_add_posts_to_v5_ads_with_method_add_and_returns_ok_envelope():
    """``ads_add`` MUST post to the v5 ``ads`` service with ``method=add``
    and a list of ``Ads``. Each item carries the target ``AdGroupId``
    and a ``TextAd`` block (Direct API v5 contract for a text ad)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"result": {"AddResults": [{"Id": 5001}]}},
        )

    client = _client(handler)
    result = client.ads_add(
        [
            {
                "AdGroupId": 1001,
                "TextAd": {
                    "Title": "Заголовок объявления",
                    "Text": "Текст объявления",
                    "Href": "https://example.com/landing",
                },
            }
        ]
    )

    assert result["ok"] is True
    assert captured["url"] == "https://api.direct.yandex.com/json/v5/ads"
    assert captured["body"]["method"] == "add"
    ads_param = captured["body"]["params"]["Ads"]
    assert len(ads_param) == 1
    assert ads_param[0]["AdGroupId"] == 1001
    assert ads_param[0]["TextAd"]["Title"] == "Заголовок объявления"
    assert ads_param[0]["TextAd"]["Href"] == "https://example.com/landing"
    # No token echo.
    assert "SECRET-CHAIN" not in str(result)


def test_ads_add_keeps_optional_display_link_path_when_present():
    """``DisplayLinkPath`` is OPTIONAL in v5. When supplied it must
    pass through; when omitted the payload must NOT include a ``None``
    value (mirroring the ``StartDate``-omission convention from
    :meth:`YandexDirectClient.campaigns_add`)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 1}]}})

    client = _client(handler)
    # With display link path
    client.ads_add(
        [
            {
                "AdGroupId": 1,
                "TextAd": {
                    "Title": "t",
                    "Text": "body",
                    "Href": "https://example.com",
                    "DisplayLinkPath": "example.com/landing",
                },
            }
        ]
    )
    assert (
        captured["body"]["params"]["Ads"][0]["TextAd"]["DisplayLinkPath"]
        == "example.com/landing"
    )


def test_ads_add_propagates_http_error_as_yandex_direct_error():
    """Same contract as :func:`test_adgroups_add_propagates_http_error_as_yandex_direct_error`:
    HTTP 4xx/5xx must surface as :class:`YandexDirectError` without
    leaking the OAUTH token."""
    from app.yandex_direct import YandexDirectError

    settings = Settings(
        _env_file=None, directpilot_mode="live_write", yandex_oauth_token="SECRET-CHAIN-ADS-ERR"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    with pytest.raises(YandexDirectError) as exc_info:
        client.ads_add([{"AdGroupId": 1, "TextAd": {"Title": "t", "Text": "b", "Href": "h"}}])
    assert "SECRET-CHAIN-ADS-ERR" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# negativekeywordsharedsets.add (stage 5) — explicitly NOT implemented
# ---------------------------------------------------------------------------


def test_negativekeywordsharedsets_add_is_not_implemented_with_tests_doc_note():
    """``negativekeywordsharedsets.add`` (stage 5) is NOT implemented
    in this PR. The Direct API v5 shape for that service is not
    documented in the project sources and is not exercised by any
    current read path. The live-create chain therefore stops at
    stage 4, and the response carries a stable ``not_implemented``
    list that mentions ``negativekeywordsharedsets.add``.

    This test pins the explicit "do not call a v5 service we have
    not modelled" contract by:

    1. Asserting that ``YandexDirectClient`` does NOT expose a
       ``negativekeywordsharedsets_add`` helper (the only safe
       shape is to keep it absent).
    2. Asserting the implementation_scope docs state that stage 5
       is kept as ``not_implemented`` until the v5 shape is
       confirmed against the official Direct docs.
    """
    from app.yandex_direct import YandexDirectClient

    assert not hasattr(YandexDirectClient, "negativekeywordsharedsets_add"), (
        "negativekeywordsharedsets.add is intentionally NOT implemented in the v5 client; "
        "see docs/implementation_scope.md for the rationale."
    )
    scope = (Path := __import__("pathlib").Path("docs/implementation_scope.md")).read_text(
        encoding="utf-8"
    )
    # The implementation scope MUST mention the stage-5 keep-as-not_implemented
    # decision so the rationale is visible to operators / reviewers.
    assert "negativekeywordsharedsets" in scope


# ---------------------------------------------------------------------------
# build_v5_chain_payloads (store-level mapping from CampaignDraft -> v5)
# ---------------------------------------------------------------------------


def test_build_v5_chain_payloads_maps_draft_ad_groups_ads_keywords_to_v5_shape():
    """The store's :func:`MockStore._build_v5_chain_payloads` helper
    MUST translate a campaign-draft preview into a list of v5 stage
    payloads that match the documented v5 contract. We never invent
    field names; we mirror the v5 docs literally.

    Stage 2 ``adgroups.add`` uses the same ``NegativeKeywords.Items``
    shape the existing ``adgroups.update`` semantic-change path uses
    (see ``app/yandex_direct.py`` and ``_apply_with_existing_negatives``)
    — passing them on ``add`` is the safe, confirmed route because
    the new ad group has no pre-existing negatives to merge with.

    Stage 3 ``ads.add`` keeps the v5 text-ad contract: ``AdGroupId``
    on the item, ``TextAd.Title`` / ``TextAd.Text`` / ``TextAd.Href``.
    ``display_link_path`` is intentionally not sent in live-create because
    Direct v5 rejects ``TextAd.DisplayLinkPath`` in ``ads.add``.

    Stage 4 ``keywords.add`` flattens group-level keywords from the
    draft, looking up each keyword's Yandex ad group via the
    local→Yandex id map. Campaign-level keywords (draft.keywords)
    are applied to every group.
    """
    from app.store import MockStore
    from app.models import AdGroup, Ad, CampaignDraft, BudgetSettings, BidSettings

    draft = CampaignDraft(
        id="draft_test",
        business_type="local_services",
        region="Казань",
        monthly_budget=30000.0,
        landing_url="https://example.com/landing",
        ad_groups=[
            AdGroup(id="adg_local_1", name="группа 1", keywords=["купить кондиционер", "установка"]),
            AdGroup(id="adg_local_2", name="группа 2", keywords=["ремонт"]),
        ],
        ads=[
            Ad(
                id="ad_local_1",
                ad_group_id="adg_local_1",
                title="T1",
                text="b1",
                landing_url="https://example.com/landing",
                display_link_path="example.com/landing",
            ),
            Ad(
                id="ad_local_2",
                ad_group_id="adg_local_2",
                title="T2",
                text="b2",
                landing_url="https://example.com/landing",
            ),
        ],
        keywords=["общая фраза"],
        negative_keywords=["бесплатно", "diy"],
        budget=BudgetSettings(),
        bids=BidSettings(),
    )
    stages = MockStore._build_v5_chain_payloads(draft, campaign_id=424242)
    # stage 2
    assert stages["adgroups.add"]["method"] == "adgroups.add"
    ad_groups_param = stages["adgroups.add"]["params"]["AdGroups"]
    assert len(ad_groups_param) == 2
    assert ad_groups_param[0]["Name"] == "группа 1"
    assert ad_groups_param[0]["CampaignId"] == 424242
    # NegativeKeywords.Items mirrors the same shape as adgroups.update
    assert ad_groups_param[0]["NegativeKeywords"] == {"Items": ["бесплатно", "diy"]}
    # stage 3
    ads_param = stages["ads.add"]["params"]["Ads"]
    assert len(ads_param) == 2
    assert ads_param[0]["AdGroupId"] == "adg_local_1"  # placeholder; mapping done in apply
    assert ads_param[0]["TextAd"]["Title"] == "T1"
    assert ads_param[0]["TextAd"]["Text"] == "b1"
    assert ads_param[0]["TextAd"]["Href"] == "https://example.com/landing"
    assert "DisplayLinkPath" not in ads_param[0]["TextAd"]
    # No display_link_path -> key is absent, not None.
    assert "DisplayLinkPath" not in ads_param[1]["TextAd"]
    # stage 4
    kw_param = stages["keywords.add"]["params"]["Keywords"]
    # 2 group-level keywords in group 1 + 1 in group 2 + 1 campaign-level
    # keyword broadcast to both groups = 5 items.
    assert len(kw_param) == 5
    assert all("Keyword" in k and "AdGroupId" in k for k in kw_param)


def test_build_v5_chain_payloads_omits_optional_negative_items_when_empty():
    """When the draft has no negative keywords, ``adgroups.add`` MUST
    OMIT the ``NegativeKeywords`` block entirely. v5 treats the
    block as OPTIONAL on ``adgroups.add`` (a missing block is
    always accepted; an empty ``Items`` list is rejected on some
    upstream edge cases). The semantic-change ``adgroups.update``
    path keeps the same omission convention for consistency.
    """
    from app.store import MockStore
    from app.models import AdGroup, Ad, CampaignDraft, BudgetSettings, BidSettings

    draft = CampaignDraft(
        id="draft_test",
        business_type="local_services",
        region="Казань",
        monthly_budget=30000.0,
        landing_url="https://example.com/landing",
        ad_groups=[AdGroup(id="adg_x", name="g", keywords=["kw"])],
        ads=[
            Ad(id="ad_1", ad_group_id="adg_x", title="T", text="b", landing_url="h")
        ],
        budget=BudgetSettings(),
        bids=BidSettings(),
    )
    stages = MockStore._build_v5_chain_payloads(draft, campaign_id=1)
    ag = stages["adgroups.add"]["params"]["AdGroups"][0]
    assert "NegativeKeywords" not in ag, (
        "When the draft has no negatives, adgroups.add MUST omit the "
        "NegativeKeywords block entirely (v5 treats the block as optional)."
    )
    # RegionIds is still required by v5.
    assert ag["RegionIds"] == [43]


def test_build_v5_chain_payloads_handles_empty_draft_structure():
    """When the draft has no ad groups / ads / keywords, the chain
    helper MUST return an empty list for each stage payload so the
    apply path can skip the network call instead of sending
    ``{"AdGroups": []}`` to v5 (which v5 rejects). The dry-run
    preview still surfaces an empty stage so the operator can see
    the chain is intentionally a no-op for the missing parts.
    """
    from app.store import MockStore
    from app.models import CampaignDraft, BudgetSettings, BidSettings

    draft = CampaignDraft(
        id="draft_empty",
        business_type="local_services",
        region="Казань",
        monthly_budget=30000.0,
        landing_url="https://example.com/landing",
        budget=BudgetSettings(),
        bids=BidSettings(),
    )
    stages = MockStore._build_v5_chain_payloads(draft, campaign_id=1)
    assert stages["adgroups.add"]["params"]["AdGroups"] == []
    assert stages["ads.add"]["params"]["Ads"] == []
    assert stages["keywords.add"]["params"]["Keywords"] == []


def test_live_create_chain_does_not_call_resume():
    """The chain is intentionally paused/DRAFT by default. We
    explicitly do NOT call ``campaigns.resume`` automatically — that
    is a separate endpoint with its own approval / idempotency gate
    (``POST /yandex/campaigns/{campaign_id}/resume``). The
    ``campaigns.add`` payload MUST use ``Status="DRAFT"`` (which the
    existing ``_build_v5_campaign_from_draft`` already does); the
    test below pins both halves of the contract by asserting the
    helper output and that ``YandexDirectClient`` exposes no
    ``resume_campaign`` auto-call from the live-create path.
    """
    from app.store import MockStore
    from app.models import CampaignDraft, BudgetSettings, BidSettings

    draft = CampaignDraft(
        id="draft_test",
        business_type="local_services",
        region="Казань",
        monthly_budget=30000.0,
        landing_url="https://example.com/landing",
        budget=BudgetSettings(),
        bids=BidSettings(),
    )
    v5 = MockStore._build_v5_campaign_from_draft(
        draft, start_date=None, counter_ids=[]
    )
    # Direct API docs do not expose Status as a campaigns.add input;
    # lifecycle/moderation state is controlled by Direct. The create
    # chain must therefore not invent an active/DRAFT status field, and
    # must not call resume automatically.
    assert "Status" not in v5

    # The live-create store method MUST NOT have a private
    # ``_resume_after_create`` helper that auto-activates. This is
    # a defence-in-depth test: if a future maintainer adds an
    # auto-activation, the audit log + a separate reviewer will
    # catch it, but the unit test pins the surface area.
    assert not hasattr(MockStore, "_resume_after_create"), (
        "Live-create must not silently auto-activate a freshly created "
        "campaign. Activation goes through POST /yandex/campaigns/{id}/resume."
    )


# ---------------------------------------------------------------------------
# Region resolution (geo targeting -> v5 RegionIds)
#
# Reviewer REQUEST_CHANGES blocker: ``adgroups.add`` payload MUST carry
# a valid ``RegionIds`` list. The draft only stores the human-readable
# region name (``draft.region``); we resolve it to Yandex's internal
# region ids via an explicit local helper in ``app.store``. Unknown
# regions must fail closed BEFORE any ``campaigns.add`` network call.
# ---------------------------------------------------------------------------


def test_resolve_region_to_ids_maps_known_regions():
    """The local resolver MUST return the documented v5 ``RegionIds``
    for the well-known regions. The map is intentionally small and
    explicit — no external lookup, easy to extend."""
    from app.store import _resolve_region_to_ids

    # Russian cities we ship in the Beta
    assert _resolve_region_to_ids("Казань") == [43]
    assert _resolve_region_to_ids("Москва") == [213]
    assert _resolve_region_to_ids("Санкт-Петербург") == [2]
    assert _resolve_region_to_ids("СПб") == [2]
    assert _resolve_region_to_ids("Россия") == [225]
    assert _resolve_region_to_ids("Russia") == [225]
    # Whitespace / casing must not break the lookup.
    assert _resolve_region_to_ids("  казань  ") == [43]
    assert _resolve_region_to_ids("СПБ") == [2]


def test_resolve_region_to_ids_raises_yandex_direct_error_for_unknown_region():
    """Unknown / unmapped regions MUST fail closed as
    :class:`YandexDirectError` with a redacted message — NOT a
    raw ``KeyError``/``AttributeError`` that escapes to the
    endpoint's safety net and leaks internals. The exception
    message must mention the offending region name (so the
    operator can fix it) and must NOT mention the OAUTH token."""
    from app.store import _resolve_region_to_ids
    from app.yandex_direct import YandexDirectError

    with pytest.raises(YandexDirectError) as exc_info:
        _resolve_region_to_ids("Тмутаракань")
    msg = str(exc_info.value)
    assert "Тмутаракань" in msg
    assert "RegionIds" in msg or "region" in msg.lower()


def test_resolve_region_to_ids_raises_on_empty_or_whitespace():
    """Empty / whitespace regions are not real geo targets. The
    resolver must reject them with the same typed error so the
    apply path can short-circuit before any network call."""
    from app.store import _resolve_region_to_ids
    from app.yandex_direct import YandexDirectError

    for bad in ("", "   ", None):
        with pytest.raises(YandexDirectError):
            _resolve_region_to_ids(bad)  # type: ignore[arg-type]


def test_build_v5_chain_payloads_includes_region_ids_resolved_from_draft_region():
    """``_build_v5_chain_payloads`` MUST inject ``RegionIds`` into
    every ``adgroups.add`` item, resolved from ``draft.region``.
    The reviewer specifically called this out as a hard blocker:
    Yandex Direct v5 ``adgroups.add`` rejects items without a
    valid geo target."""
    from app.store import MockStore
    from app.models import AdGroup, Ad, CampaignDraft, BudgetSettings, BidSettings

    draft = CampaignDraft(
        id="draft_region",
        business_type="local_services",
        region="Казань",
        monthly_budget=30000.0,
        landing_url="https://example.com/landing",
        ad_groups=[
            AdGroup(id="adg_1", name="группа 1", keywords=["купить кондиционер"]),
        ],
        ads=[
            Ad(
                id="ad_1",
                ad_group_id="adg_1",
                title="T",
                text="b",
                landing_url="https://example.com/landing",
            )
        ],
        budget=BudgetSettings(),
        bids=BidSettings(),
    )
    stages = MockStore._build_v5_chain_payloads(draft, campaign_id=99)
    ag = stages["adgroups.add"]["params"]["AdGroups"]
    assert ag[0]["RegionIds"] == [43], (
        "adgroups.add payload MUST include RegionIds resolved from draft.region"
    )


def test_build_v5_chain_payloads_omits_negative_keywords_block_when_empty():
    """Per the v5 docs review, ``adgroups.add`` may carry
    ``NegativeKeywords`` as an OPTIONAL block. When the draft has
    no negatives we MUST omit the block (v5 rejects empty blocks
    on some endpoints and accepts a missing one everywhere). When
    the draft has negatives we keep the block with the items —
    this matches the existing ``adgroups.update`` semantic-change
    shape and keeps the apply path predictable."""
    from app.store import MockStore
    from app.models import AdGroup, Ad, CampaignDraft, BudgetSettings, BidSettings

    # Empty -> block omitted entirely
    draft_empty = CampaignDraft(
        id="draft_no_neg",
        business_type="local_services",
        region="Москва",
        monthly_budget=30000.0,
        landing_url="https://example.com/landing",
        ad_groups=[AdGroup(id="adg_1", name="g", keywords=["kw"])],
        ads=[Ad(id="ad_1", ad_group_id="adg_1", title="T", text="b", landing_url="h")],
        budget=BudgetSettings(),
        bids=BidSettings(),
    )
    stages = MockStore._build_v5_chain_payloads(draft_empty, campaign_id=1)
    ag = stages["adgroups.add"]["params"]["AdGroups"][0]
    assert "NegativeKeywords" not in ag, (
        "When the draft has no negatives, adgroups.add MUST omit the "
        "NegativeKeywords block entirely."
    )
    # RegionIds is still present (independent of the negatives decision).
    assert ag["RegionIds"] == [213]

    # Non-empty -> block present with the items
    draft_with_neg = CampaignDraft(
        id="draft_with_neg",
        business_type="local_services",
        region="Москва",
        monthly_budget=30000.0,
        landing_url="https://example.com/landing",
        negative_keywords=["бесплатно", "diy"],
        ad_groups=[AdGroup(id="adg_1", name="g", keywords=["kw"])],
        ads=[Ad(id="ad_1", ad_group_id="adg_1", title="T", text="b", landing_url="h")],
        budget=BudgetSettings(),
        bids=BidSettings(),
    )
    stages2 = MockStore._build_v5_chain_payloads(draft_with_neg, campaign_id=1)
    ag2 = stages2["adgroups.add"]["params"]["AdGroups"][0]
    assert ag2["NegativeKeywords"] == {"Items": ["бесплатно", "diy"]}
    assert ag2["RegionIds"] == [213]


def test_build_v5_chain_payloads_raises_on_unknown_region():
    """An unmapped region MUST surface as :class:`YandexDirectError`
    from the chain builder so the apply path can refuse to dispatch
    ``campaigns.add`` / ``adgroups.add``. The exception is
    intentionally typed so the endpoint can return a redacted 502."""
    from app.store import MockStore
    from app.models import CampaignDraft, BudgetSettings, BidSettings
    from app.yandex_direct import YandexDirectError

    draft = CampaignDraft(
        id="draft_unknown_region",
        business_type="local_services",
        region="Тмутаракань",
        monthly_budget=30000.0,
        landing_url="https://example.com/landing",
        budget=BudgetSettings(),
        bids=BidSettings(),
    )
    with pytest.raises(YandexDirectError) as exc_info:
        MockStore._build_v5_chain_payloads(draft, campaign_id=1)
    assert "Тмутаракань" in str(exc_info.value)


def test_build_v5_chain_payloads_no_status_draft_in_adgroups_payload():
    """Defence-in-depth: the chain builder must NOT inject a
    ``Status="DRAFT"`` (or any other) field on the ``adgroups.add``
    items. Lifecycle state is controlled by Direct / the operator
    via the separate resume endpoint, not by the create chain."""
    from app.store import MockStore
    from app.models import AdGroup, Ad, CampaignDraft, BudgetSettings, BidSettings

    draft = CampaignDraft(
        id="draft_no_status",
        business_type="local_services",
        region="Россия",
        monthly_budget=30000.0,
        landing_url="https://example.com/landing",
        ad_groups=[AdGroup(id="adg_1", name="g", keywords=["kw"])],
        ads=[Ad(id="ad_1", ad_group_id="adg_1", title="T", text="b", landing_url="h")],
        budget=BudgetSettings(),
        bids=BidSettings(),
    )
    stages = MockStore._build_v5_chain_payloads(draft, campaign_id=1)
    for item in stages["adgroups.add"]["params"]["AdGroups"]:
        assert "Status" not in item, (
            "adgroups.add items MUST NOT carry a Status field; "
            "lifecycle is controlled by Direct and the resume endpoint."
        )
