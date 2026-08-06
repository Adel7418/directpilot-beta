from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


# ---------------------------------------------------------------------------
# Campaigns (mock + Yandex read-only facade)
# ---------------------------------------------------------------------------


class Campaign(BaseModel):
    id: str
    name: str
    source: Literal["mock", "yandex"] = "mock"
    business_type: str
    status: str
    spend: float = 0.0
    clicks: int = 0


class CampaignList(BaseModel):
    items: list[Campaign]


class YandexCampaign(BaseModel):
    id: str
    name: str
    status: str
    type: str
    daily_budget: float


class YandexCampaignList(BaseModel):
    items: list[YandexCampaign]
    source: Literal["mock", "yandex"] = "mock"
    read_only: bool = True


class ReportSummary(BaseModel):
    period: str = "last_7_days"
    spend: float
    clicks: int
    impressions: int
    ctr: float
    cpc: float
    conversions: int | None = None
    cpa: float | None = None
    source: Literal["mock", "yandex"] = "mock"
    read_only: bool = True


# ---------------------------------------------------------------------------
# Campaign drafts (constructor)
# ---------------------------------------------------------------------------


class CampaignDraftRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    business_type: str = Field(..., min_length=2)
    region: str = Field(..., min_length=2)
    monthly_budget: float = Field(..., gt=0)
    landing_url: str
    utm_config: UtmConfig | None = Field(
        default=None,
        description="Optional UTM configuration — when enabled, ads get UTM-tagged Hrefs.",
    )


class CampaignDraftBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    business_type: str | None = Field(default=None, min_length=2)
    region: str | None = Field(default=None, min_length=2)
    monthly_budget: float | None = Field(default=None, gt=0)
    landing_url: str | None = None


class AdGroup(BaseModel):
    id: str
    name: str
    keywords: list[str] = Field(default_factory=list)


class AdGroupCreate(BaseModel):
    name: str = Field(..., min_length=1)
    keywords: list[str] = Field(default_factory=list)


class AdGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    keywords: list[str] | None = None


class Ad(BaseModel):
    id: str
    ad_group_id: str
    title: str
    text: str
    landing_url: str
    display_link_path: str | None = None


class AdCreate(BaseModel):
    ad_group_id: str
    title: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    landing_url: str
    display_link_path: str | None = None


class AdUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    text: str | None = Field(default=None, min_length=1)
    landing_url: str | None = None
    display_link_path: str | None = None


class BudgetSettings(BaseModel):
    daily_budget: float | None = Field(default=None, ge=0)
    monthly_budget: float | None = Field(default=None, ge=0)
    strategy: Literal["manual", "max_clicks", "max_conversions", "weekly_budget"] = "manual"
    # DailyBudget.Mode on the v5 ``campaigns.add`` payload. Direct v5
    # rejects a DailyBudget block without a Mode with error_code=8000
    # ("Отсутствует обязательный параметр Mode"). We default to
    # ``STANDARD`` — the documented spend mode for a freshly created
    # campaign — and let the operator override it before launch via
    # ``PATCH /campaign-drafts/{id}/budget``.
    daily_budget_mode: Literal["STANDARD", "DISTRIBUTED", "STAY_IN_DEFAULT_BUDGET"] = "STANDARD"


class BudgetUpdate(BaseModel):
    daily_budget: float | None = Field(default=None, ge=0)
    monthly_budget: float | None = Field(default=None, ge=0)
    strategy: Literal["manual", "max_clicks", "max_conversions", "weekly_budget"] | None = None
    daily_budget_mode: (
        Literal["STANDARD", "DISTRIBUTED", "STAY_IN_DEFAULT_BUDGET"] | None
    ) = None


class BidSettings(BaseModel):
    max_cpc: float | None = Field(default=None, gt=0)
    keyword_bids: dict[str, float] = Field(default_factory=dict)


class BidUpdate(BaseModel):
    max_cpc: float | None = Field(default=None, gt=0)
    keyword_bids: dict[str, float] | None = None


class CampaignDraftKeywordsUpdate(BaseModel):
    keywords: list[str] = Field(..., min_length=1)


class CampaignDraftKeywordsAdd(BaseModel):
    keywords: list[str] = Field(..., min_length=1)


class CampaignDraftKeywordsRemove(BaseModel):
    keywords: list[str] = Field(..., min_length=1)


class NegativeKeywordsReplace(BaseModel):
    negative_keywords: list[str] = Field(..., min_length=1)


class GenerateStructureRequest(BaseModel):
    topic: str = Field(..., min_length=2)
    region: str = Field(..., min_length=2)
    group_count: int = Field(default=3, ge=1, le=10)
    keywords_per_group: int = Field(default=5, ge=1, le=20)


class GenerateStructureResult(BaseModel):
    ad_groups: list[AdGroup]
    keywords: list[str]
    negative_keywords: list[str]
    ads: list[Ad]


class ValidationIssue(BaseModel):
    code: str
    severity: Literal["error", "warning", "info"]
    message: str


class ValidationResult(BaseModel):
    valid: bool
    issues: list[ValidationIssue]


class PreviewPayload(BaseModel):
    draft_id: str
    name: str
    business_type: str
    region: str
    landing_url: str
    budget: BudgetSettings
    bids: BidSettings
    ad_groups: list[AdGroup]
    ads: list[Ad]
    negative_keywords: list[str]
    yandex_payload: dict
    dry_run: bool = True
    requires_approval: bool = True


class CampaignDraft(BaseModel):
    id: str
    status: Literal["draft"] = "draft"
    name: str | None = None
    business_type: str
    region: str
    monthly_budget: float
    landing_url: str
    keywords: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    ad_groups: list[AdGroup] = Field(default_factory=list)
    ads: list[Ad] = Field(default_factory=list)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    bids: BidSettings = Field(default_factory=BidSettings)
    risk_level: Literal["low", "medium", "high"] = "medium"
    requires_approval: bool = True
    utm_config: UtmConfig | None = Field(
        default=None,
        description="UTM configuration stored from draft creation. Used by preview and live-create.",
    )


class CampaignDraftList(BaseModel):
    items: list[CampaignDraft]


# ---------------------------------------------------------------------------
# Recommendations / approval / apply
# ---------------------------------------------------------------------------


class Recommendation(BaseModel):
    id: str
    action_id: str
    reason: str
    risk_level: Literal["low", "medium", "high"]
    status: Literal["pending", "approved", "rejected", "applied"] = "pending"
    requires_approval: bool = True


class RecommendationList(BaseModel):
    items: list[Recommendation]


class ApprovalResult(BaseModel):
    recommendation_id: str
    status: Literal["approved", "rejected"]


class ApplyActionRequest(BaseModel):
    dry_run: bool = True
    approved: bool
    idempotency_key: str = Field(..., min_length=6)


class ApplyActionResult(BaseModel):
    action_id: str
    dry_run: bool
    applied: bool
    risk_level: Literal["low", "medium", "high"]
    audit_id: str


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class AuditEvent(BaseModel):
    id: str
    actor: str
    action: str
    entity: str
    dry_run: bool = True
    details: dict | None = None


class AuditLog(BaseModel):
    items: list[AuditEvent]


# ---------------------------------------------------------------------------
# UTM / budget simulation
# ---------------------------------------------------------------------------


class UtmConfig(BaseModel):
    """Optional UTM configuration for campaign drafts and live-create.

    When ``enabled=True``, the UTM builder is applied to every ad's Href
    at creation time (campaign draft preview + live-create payload).

    ``campaign_slug`` — if not provided, is generated from draft name + id
    (same logic as the UTM audit/plan/apply endpoints).

    ``overwrite`` — if False (default), existing UTM params on the landing
    URL are preserved. If True, all existing UTM params are replaced.

    ``custom_params`` — additional query params injected after core UTM;
    never override the core five (utm_source/medium/campaign/content/term).

    ``enabled`` — master switch. When False (default), the draft/live-create
    flow builds ads with the raw landing URL unchanged.
    """

    enabled: bool = False
    campaign_slug: str | None = Field(
        default=None,
        min_length=1,
        description="Slug for utm_campaign. Generated from draft name+id when omitted.",
    )
    overwrite: bool = Field(
        default=False,
        description="Replace existing UTM params when True; preserve when False.",
    )
    custom_params: dict[str, str] | None = Field(
        default=None,
        description="Additional query params injected after core UTM.",
    )


class UtmGenerateRequest(BaseModel):
    landing_url: HttpUrl
    campaign: str = Field(..., min_length=1)
    content: str = Field(default="{ad_id}", min_length=1)
    term: str = Field(default="{keyword}", min_length=1)


class UtmGenerateResult(BaseModel):
    url: str
    requires_approval: bool = False


class UtmAuditItem(BaseModel):
    """Single URL UTM audit result for an ad or sitelink."""

    entity_type: Literal["ad", "sitelink"]
    entity_id: str
    url: str
    utm_status: Literal["complete", "partial", "missing", "mismatch"]
    present_params: dict[str, str] = Field(default_factory=dict)
    missing_params: list[str] = Field(default_factory=list)
    wrong_values: dict[str, Any] = Field(default_factory=dict)


class UtmAuditResult(BaseModel):
    """Response for ``GET /yandex/campaigns/{campaign_id}/utm-audit``."""

    campaign_id: str
    source: Literal["mock", "yandex"] = "mock"
    read_only: bool = True
    ads_total: int = 0
    sitelinks_total: int = 0
    complete_count: int = 0
    partial_count: int = 0
    missing_count: int = 0
    mismatch_count: int = 0
    items: list[UtmAuditItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class UtmPlanRequest(BaseModel):
    """Body of ``POST /yandex/campaigns/{campaign_id}/utm-plan``.

    Always dry_run — never mutates Yandex state. Returns the URLs
    that WOULD be changed with the proposed UTM params.
    """

    campaign_slug: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Slug for utm_campaign. If not provided, generated from "
            "campaign name + id (safe, unambiguous)."
        ),
    )
    overwrite: bool = Field(
        default=False,
        description="Replace existing UTM params when True; preserve existing when False.",
    )
    custom_params: dict[str, str] | None = Field(
        default=None,
        description=(
            "Additional query params to inject (e.g. utm_custom=extra, ref=dp). "
            "Never override the five core UTM params."
        ),
    )
    include_sitelinks: bool = Field(
        default=True,
        description="Include sitelinks in the plan and apply flow.",
    )


class UtmChangeItem(BaseModel):
    """One planned URL change: old → new with UTM."""

    entity_type: Literal["ad", "sitelink"]
    entity_id: str
    old_url: str
    new_url: str
    utm_status_before: Literal["complete", "partial", "missing", "mismatch"]


class UtmPlanResult(BaseModel):
    """Response for ``POST /yandex/campaigns/{campaign_id}/utm-plan``.

    Always ``dry_run=True``, ``applied=False``, no network writes.
    """

    campaign_id: str
    source: Literal["mock", "yandex"] = "mock"
    dry_run: bool = True
    applied: bool = False
    campaign_slug: str = ""
    items: list[UtmChangeItem] = Field(default_factory=list)
    sitelink_items: list[UtmChangeItem] = Field(
        default_factory=list,
        description="Sitelink URL changes produced for preview and optional apply.",
    )
    payload_preview: dict | None = Field(
        default=None,
        description="The v5 ``ads.update`` payload that WOULD be sent on apply.",
    )
    warnings: list[str] = Field(default_factory=list)
    not_implemented: list[str] = Field(default_factory=list)


class UtmApplyRequest(BaseModel):
    """Body of ``POST /yandex/campaigns/{campaign_id}/utm-apply``.

    Write-gate contract (mandatory):
    * ``dry_run=True`` — preview only, never mutates Yandex state.
    * ``dry_run=False`` requires ALL of:
      1. ``DIRECTPILOT_MODE=live_write``
      2. ``approved=true``
      3. ``idempotency_key`` (>= 6 chars)
    """

    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True
    campaign_slug: str | None = Field(
        default=None,
        min_length=1,
        description="Slug for utm_campaign param.",
    )
    overwrite: bool = Field(
        default=False,
        description="Replace existing UTM when True.",
    )
    custom_params: dict[str, str] | None = Field(
        default=None,
        description="Additional query params injected after core UTM.",
    )
    include_sitelinks: bool = Field(
        default=True,
        description="Request sitelink URL changes and apply them together with ads when requested.",
    )
    reason: str | None = Field(
        default=None,
        description="Human-readable reason for the audit log.",
    )


class UtmApplyResult(BaseModel):
    """Response for ``POST /yandex/campaigns/{campaign_id}/utm-apply``.

    * ``dry_run=True`` → ``applied=False``, ``ad_ids=[]``,
      ``payload_preview`` populated.
    * ``dry_run=False`` + ``live_write`` → ``applied=True``,
      ``ad_ids`` populated, ``readback`` may be populated.
    """

    campaign_id: str
    source: Literal["mock", "yandex"] = "yandex"
    mode: str
    dry_run: bool
    applied: bool
    audit_id: str
    campaign_slug: str = ""
    ad_ids: list[int] = Field(default_factory=list)
    sitelink_items: list[UtmChangeItem] = Field(
        default_factory=list,
        description="Sitelink URL changes when include_sitelinks=True; may be empty.",
    )
    payload_preview: dict | None = None
    readback: list[dict] | None = None
    sitelink_readback: list[dict] | None = Field(
        default=None,
        description="Explicit readback of updated sitelink URLs after successful apply.",
    )
    provider_warnings: list["ProviderWarning"] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    not_implemented: list[str] = Field(default_factory=list)
    yandex_units: int | None = None
    yandex_error: str | None = None


class BudgetSimulationRequest(BaseModel):
    daily_budget: float = Field(..., gt=0)
    avg_cpc: float = Field(..., gt=0)
    conversion_rate: float = Field(..., ge=0, le=100)


class BudgetSimulationResult(BaseModel):
    dry_run: bool = True
    estimated_clicks: int
    estimated_conversions: float
    estimated_spend: float


class AuditCheck(BaseModel):
    code: str
    severity: Literal["low", "medium", "high"]
    title: str
    recommendation: str


class CampaignAuditResult(BaseModel):
    items: list[AuditCheck]


# ---------------------------------------------------------------------------
# Yandex Direct read-only facade (deterministic mock)
# ---------------------------------------------------------------------------


class YandexAdGroup(BaseModel):
    id: str
    campaign_id: str
    name: str
    status: str


class YandexAdGroupList(BaseModel):
    items: list[YandexAdGroup]
    source: Literal["mock", "yandex"] = "mock"
    read_only: bool = True


class YandexAd(BaseModel):
    id: str
    ad_group_id: str
    campaign_id: str
    title: str
    status: str


class YandexAdList(BaseModel):
    items: list[YandexAd]
    source: Literal["mock", "yandex"] = "mock"
    read_only: bool = True


class YandexKeyword(BaseModel):
    id: str
    ad_group_id: str
    phrase: str
    status: str


class YandexKeywordList(BaseModel):
    items: list[YandexKeyword]
    source: Literal["mock", "yandex"] = "mock"
    read_only: bool = True


class YandexSearchQuery(BaseModel):
    query: str
    campaign_id: str
    campaign_name: str | None = None
    ad_group_id: str
    impressions: int
    clicks: int
    ctr: float
    cost: float | None = None


class YandexSearchQueriesReport(BaseModel):
    period: str
    items: list[YandexSearchQuery]
    source: Literal["mock", "yandex"] = "mock"
    read_only: bool = True


class YandexRawResult(BaseModel):
    service: str
    method: str
    data: Any
    source: Literal["mock", "yandex"] = "yandex"
    read_only: bool = True


# ---------------------------------------------------------------------------
# Campaign ad-assets models (read-only, marketing/audit)
# ---------------------------------------------------------------------------


class YandexAdAssetItem(BaseModel):
    """Single ad with extended marketing/asset fields."""

    id: str
    ad_group_id: str
    campaign_id: str = ""
    status: str = "UNKNOWN"
    state: str = "UNKNOWN"
    type: str = "TEXT_AD"
    title: str = ""
    title2: str | None = None
    text: str = ""
    href: str = ""
    display_url_path: str | None = None
    sitelink_set_id: str | None = None
    business_id: str | None = None
    vcard_id: str | None = None
    prefer_vcard_over_business: str | None = None
    ad_extension_ids: list[int] | None = None


class YandexSitelinkItem(BaseModel):
    """Individual sitelink within a sitelink set."""

    title: str
    href: str | None = None
    description: str | None = None


class YandexSitelinkSetItem(BaseModel):
    """Sitelink set resolved from Direct."""

    id: str
    sitelinks: list[YandexSitelinkItem] = []


class YandexBusinessAssetItem(BaseModel):
    """Business profile asset."""

    id: str
    name: str = ""
    address: str | None = None


class YandexVCardAssetItem(BaseModel):
    """VCard asset."""

    id: str
    company_name: str = ""
    phone: str | None = None


class YandexAdAssetsMissing(BaseModel):
    """Explicit gaps for features not yet implemented."""

    callouts: str = "not_implemented: Direct API v5 callouts (AdExtensions) read method is not yet mapped"


class YandexAdAssetsResult(BaseModel):
    """Aggregated campaign ad-assets response."""

    campaign_id: str
    source: Literal["mock", "yandex"] = "mock"
    read_only: bool = True
    ads: list[YandexAdAssetItem] = []
    sitelinks_sets: list[YandexSitelinkSetItem] = []
    businesses: list[YandexBusinessAssetItem] = []
    vcards: list[YandexVCardAssetItem] = []
    callouts: list[Any] = []
    missing: YandexAdAssetsMissing = YandexAdAssetsMissing()


# ---------------------------------------------------------------------------


class YandexVCardPhone(BaseModel):
    country_code: str = Field(default="7", min_length=1)
    city_code: str = Field(..., min_length=2)
    phone_number: str = Field(..., min_length=5)
    extension: str | None = None


class YandexVCardRequest(BaseModel):
    """Safe Direct vCard create request.

    ``dry_run=True`` never calls Yandex. A real vCard create requires
    ``approved=True``, ``dry_run=False``, ``idempotency_key`` and DirectPilot
    mode ``sandbox`` or ``live_write``.
    """

    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True
    campaign_id: int | str | None = Field(
        default=None,
        description="Direct campaign id required by vcards.add for real Yandex writes",
    )
    company_name: str = Field(..., min_length=1)
    country: str = Field(default="Россия", min_length=2)
    city: str = Field(default="Казань", min_length=2)
    phone: YandexVCardPhone
    work_time: str = Field(
        default="0#6#07#00#24#00",
        description="Yandex Direct WorkTime format, e.g. daily 07:00-24:00",
    )
    contact_person: str | None = None
    street: str | None = None
    house: str | None = None
    building: str | None = None
    apartment: str | None = None
    extra_message: str | None = None
    reason: str | None = None


class YandexVCardResult(BaseModel):
    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "mock"
    audit_id: str
    vcard_id: str | None = None
    work_time: str


# ---------------------------------------------------------------------------
# Yandex Direct ads.update — BusinessId attach
# ---------------------------------------------------------------------------
#
# The BusinessId-attach path is the safe-by-default way to attach an
# existing Yandex Business organization (verified via
# ``businesses.get``) to one or more TextAd objects. The endpoint sets
# two v5 fields on each target ad:
#
# * ``TextAd.BusinessId`` — long
# * ``TextAd.PreferVCardOverBusiness`` — ``"NO"`` (literal string per v5)
#
# Direct API v5 ``ads.update`` is a REPLACE-shaped call: every field
# the operator wants to keep on the ad MUST be re-sent in the same
# request. The store layer reads the live ad via ``ads.get`` and
# forwards ``Title`` / ``Text`` / ``Href`` together with the new
# ``BusinessId`` / ``PreferVCardOverBusiness`` fields.
#
# This is preferred over ``vcards.add`` for organization-level contact
# information because ``vcards.add`` can fail with ``error_code=3500``
# for several account types — see ``docs/API_SIMPLE.md`` for the
# rationale.


class YandexAdsBusinessAttachRequest(BaseModel):
    """Body of ``POST /yandex/ads/business``.

    Exactly one of ``ad_ids`` or ``campaign_id`` is required. When
    ``campaign_id`` is supplied, the store reads the campaign's ads
    via ``ads.get`` and targets only TextAds (``Ad.Type == "TEXT_AD"``).
    Other ad types (e.g. ``IMAGE_AD``) are SKIPPED and surfaced in
    the response so the operator can see which ads were excluded.

    ``business_id`` is the Yandex Business id (a long). The store
    NEVER validates that the business exists — that is the
    operator's job before calling this endpoint. A bad ``business_id``
    surfaces as a v5 ``error_code`` in the audit log + response.
    """

    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True
    business_id: int = Field(..., ge=1)
    ad_ids: list[int] | None = None
    campaign_id: int | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _require_at_least_one_target(self) -> "YandexAdsBusinessAttachRequest":
        if not self.ad_ids and self.campaign_id is None:
            raise ValueError(
                "Either ad_ids or campaign_id is required for /yandex/ads/business"
            )
        return self


class YandexAdsBusinessAttachSkipped(BaseModel):
    """One ad the store decided NOT to attach the BusinessId to.

    The most common reason is ``not_text_ad`` (e.g. an ``IMAGE_AD``
    was returned by ``ads.get`` for the same campaign). The list is
    always returned — empty when every target ad was a TextAd — so
    the operator can see exactly which ads were excluded and why.
    """

    ad_id: int
    reason: str


class YandexAdsBusinessAttachResult(BaseModel):
    """Response envelope for the BusinessId attach.

    * ``dry_run=True`` returns the v5 ``ads.update`` payload that
      WOULD be sent, with ``applied=False`` and ``ad_ids=[]``.
    * ``dry_run=False`` + ``live_write`` + ``approved=True`` +
      ``idempotency_key`` performs the real v5 ``ads.update`` call
      and returns the targeted ad ids with ``applied=True`` and
      ``source="yandex"``. The cached result is replayed when the
      same ``idempotency_key`` is sent again.
    * ``source="mock"`` is reserved for the mock-mode dry-run path.
    """

    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "yandex"
    mode: str
    audit_id: str
    business_id: int
    ad_ids: list[int] = Field(default_factory=list)
    skipped: list[YandexAdsBusinessAttachSkipped] = Field(default_factory=list)
    payload_preview: dict | None = None
    yandex_units: int | None = None
    yandex_error: str | None = None


# ---------------------------------------------------------------------------
# Live existing-campaign ads — add ads to an existing live ad group
# ---------------------------------------------------------------------------


class LiveAdCreateRequest(BaseModel):
    """Body of ``POST /yandex/ad-groups/{ad_group_id}/ads``.

    Adds one or more text ads to an existing live Yandex Direct ad group
    via v5 ``ads.add``. Follows the standard product gate contract:

    * ``dry_run=True`` (default) returns a redacted payload preview
      and never touches the network.
    * ``dry_run=False`` requires ``approved=True``, ``idempotency_key``
      (length >= 6), and ``DIRECTPILOT_MODE=live_write``.
    * ``live_readonly`` / ``sandbox`` / ``mock`` with ``dry_run=False``
      are rejected BEFORE any network call.

    Fields follow Direct v5 ``ads.add`` TextAd shape:
    ``Title``, ``Text``, ``Href`` are required. ``Title2``,
    ``SitelinkSetId``, ``BusinessId``, ``PreferVCardOverBusiness``
    are optional. ``DisplayLinkPath`` is intentionally omitted —
    current Direct v5 rejects it as unknown on add.
    """

    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True
    ads: list["LiveAdCreateItem"] = Field(..., min_length=1)
    reason: str | None = None


class LiveAdCreateItem(BaseModel):
    """One ad to add to an existing ad group.

    ``title``, ``text``, ``href`` are required (Direct v5 TextAd contract).
    ``title2`` — optional second headline.
    ``sitelink_set_id`` — optional quick-link set id.
    ``business_id`` — optional Yandex Business organization id.
    ``prefer_vcard_over_business`` — ``\"YES\"`` / ``\"NO\"``, optional.
    """

    title: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    href: str = Field(..., min_length=1)
    title2: str | None = None
    sitelink_set_id: int | None = None
    business_id: int | None = None
    prefer_vcard_over_business: Literal["YES", "NO"] | None = None


class LiveAdCreateWarning(BaseModel):
    """Non-blocking warning / preflight question."""

    code: str
    message: str
    severity: Literal["warning", "info"] = "warning"


class ProviderWarning(BaseModel):
    """Upstream Yandex Direct API v5 warning item.

    Preserved from the v5 ``Warnings[]`` response envelope with
    ``code``, ``message``, and ``details`` (the per-warning text
    Direct attaches; redacted — never contains headers or tokens).
    """

    code: int
    message: str
    details: str = ""


class LiveAdCreateResult(BaseModel):
    """Response for ``POST /yandex/ad-groups/{ad_group_id}/ads``.

    * ``dry_run=True`` returns ``applied=False`` with
      ``payload_preview`` (redacted).
    * ``dry_run=False`` + ``live_write`` returns ``applied=True``
      with ``ad_ids``, ``add_results`` and optional ``readback``.
    * ``source=\"mock\"`` only in mock-mode dry-run.
    """

    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "yandex"
    mode: str
    audit_id: str
    ad_group_id: str
    ad_ids: list[int] = Field(default_factory=list)
    add_results: list[dict] | None = None
    readback: list[dict] | None = None
    payload_preview: dict | None = None
    warnings: list["LiveAdCreateWarning"] = Field(default_factory=list)
    provider_warnings: list["ProviderWarning"] = Field(default_factory=list)
    yandex_units: int | None = None
    yandex_error: str | None = None



# ---------------------------------------------------------------------------
# Live existing-campaign ad groups — group negatives + adgroups.add
# ---------------------------------------------------------------------------


class YandexAdGroupNegativeKeywords(BaseModel):
    ad_group_id: str
    campaign_id: str
    name: str
    status: str
    negative_keywords: list[str] = Field(default_factory=list)
    has_negative_keywords: bool = False
    source: Literal["mock", "yandex"] = "mock"
    read_only: bool = True


class YandexAdGroupNegativeKeywordsList(BaseModel):
    items: list[YandexAdGroupNegativeKeywords]
    source: Literal["mock", "yandex"] = "mock"
    read_only: bool = True


class YandexAdGroupNegativeKeywordsRequest(BaseModel):
    negative_keywords: list[str] = Field(..., min_length=1)
    operation: Literal["add", "replace"] = "add"
    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True
    reason: str | None = None


class YandexAdGroupNegativeKeywordsResult(BaseModel):
    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "yandex"
    mode: str
    audit_id: str
    campaign_id: str
    ad_group_id: str
    operation: Literal["add", "replace"]
    negative_keywords: list[str] = Field(default_factory=list)
    previous_negative_keywords: list[str] = Field(default_factory=list)
    payload_preview: dict | None = None
    provider_response: dict | None = None


class LiveAdGroupCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    region_ids: list[int] = Field(..., min_length=1)
    negative_keywords: list[str] = Field(default_factory=list)
    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True
    reason: str | None = None


class LiveAdGroupCreateResult(BaseModel):
    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "yandex"
    mode: str
    audit_id: str
    campaign_id: str
    ad_group_ids: list[int] = Field(default_factory=list)
    payload_preview: dict | None = None
    add_results: list[dict] | None = None
    provider_response: dict | None = None
    readback: list[dict] | None = None
    warnings: list[str] = Field(default_factory=list)

# ---------------------------------------------------------------------------
# ads.moderate — send ads to moderation
# ---------------------------------------------------------------------------


class AdsModerateRequest(BaseModel):
    """Body of ``POST /yandex/ads/moderate``.

    Sends one or more ads to moderation via Direct v5 ``ads.moderate``.
    Gate contract: same as other write endpoints —
    ``dry_run=True`` is preview-only; ``dry_run=False`` requires
    ``approved=True``, ``idempotency_key``, ``DIRECTPILOT_MODE=live_write``.
    """

    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True
    ad_ids: list[int] = Field(..., min_length=1)
    reason: str | None = None


class AdsModerateResult(BaseModel):
    """Response for ``POST /yandex/ads/moderate``."""

    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "yandex"
    mode: str
    audit_id: str
    ad_ids: list[int] = Field(default_factory=list)
    moderate_results: list[dict] | None = None
    readback: list[dict] | None = None
    payload_preview: dict | None = None
    provider_warnings: list["ProviderWarning"] = Field(default_factory=list)
    yandex_units: int | None = None
    yandex_error: str | None = None


class YandexSearchApiResult(BaseModel):
    """Envelope for the Yandex AI Studio / Search API v2 endpoints.

    Distinct from :class:`YandexRawResult` because the ``source`` tag is
    different (``yandex_search_api`` rather than ``yandex``) and the
    service name is the v2 wordstat family.
    """

    service: str = "wordstat"
    method: str
    data: Any
    source: Literal["yandex_search_api"] = "yandex_search_api"
    read_only: bool = True


class ApiErrorDetail(BaseModel):
    error_type: str
    message: str


class ApiErrorResponse(BaseModel):
    detail: ApiErrorDetail


# ---------------------------------------------------------------------------
# Yandex Direct Live v4 account balance
# ---------------------------------------------------------------------------


class YandexAccountBalance(BaseModel):
    """One block from a Live v4 AccountManagement → Get response.

    Exposes the four fields the user needs to decide whether the account
    can keep serving impressions: the current ``Amount``, the
    ``AmountAvailableForTransfer``, the ``Currency`` and the optional
    ``AccountDayBudget`` block.
    """

    login: str | None = None
    amount: float = 0.0
    amount_available_for_transfer: float = 0.0
    currency: str | None = None
    account_day_budget_amount: float | None = None
    account_day_budget_spend_mode: str | None = None
    raw: dict | None = None


class YandexAccountBalanceResult(BaseModel):
    accounts: list[YandexAccountBalance]
    source: Literal["yandex"] = "yandex"
    read_only: bool = True


# ---------------------------------------------------------------------------
# Yandex Direct campaign finance (v5 campaigns.get with Funds/Statistics)
# ---------------------------------------------------------------------------


class YandexCampaignFinance(BaseModel):
    """One row from a finance-shaped v5 campaigns.get response.

    Surfaces both the raw micro-unit value (``*_micros`` ints) and the
    display float (``*``) for money fields so the caller can pick
    whichever representation they need. The micro-unit values are the
    canonical ones — the display floats are derived.
    """

    id: str
    name: str
    status: str
    state: str
    type: str
    daily_budget_micros: int = 0
    daily_budget: float = 0.0
    funds_balance_micros: int = 0
    funds_balance: float = 0.0
    spend_micros: int = 0
    spend: float = 0.0
    statistics_shows: int = 0
    statistics_clicks: int = 0
    start_date: str | None = None
    end_date: str | None = None


class YandexCampaignFinanceList(BaseModel):
    items: list[YandexCampaignFinance]
    source: Literal["yandex"] = "yandex"
    read_only: bool = True


# ---------------------------------------------------------------------------
# Yandex Metrika read-only envelopes
# ---------------------------------------------------------------------------


class YandexMetrikaResult(BaseModel):
    """Read-only envelope for the Metrika Management and Stats APIs.

    Distinct from :class:`YandexRawResult` because the ``source`` tag is
    ``yandex_metrika`` rather than ``yandex`` — the Metrika APIs are a
    separate service with a separate OAUTH token.
    """

    service: str
    method: str
    counter_id: int | str | None = None
    data: Any
    source: Literal["yandex_metrika"] = "yandex_metrika"
    read_only: bool = True


# ---------------------------------------------------------------------------
# Yandex Direct control facade (pause/resume)
# ---------------------------------------------------------------------------


class YandexControlRequest(BaseModel):
    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True
    reason: str | None = None


class YandexControlResult(BaseModel):
    campaign_id: str
    action: Literal["pause", "resume"]
    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "mock"
    audit_id: str
    new_status: str


# ---------------------------------------------------------------------------
# Semantic change package (negative + positive keywords for an existing
# Yandex Direct campaign, e.g. 710382063). Designed to be safe-by-default:
# ``prepare`` is pure-local and always allowed; ``apply`` is gated by
# ``approved`` / ``idempotency_key`` / ``dry_run`` and the runtime mode.
# ---------------------------------------------------------------------------


class SemanticChangeRequest(BaseModel):
    """Body of ``POST /campaigns/{campaign_id}/semantic-changes``.

    All fields are optional but at least one of them must be present.
    The user is preparing a list of operations that will be turned into
    Direct API v5 ``keywords`` / ``adgroups`` request bodies on apply.

    ``ad_group_id`` is REQUIRED whenever either list (``add_keywords`` or
    ``add_negative_keywords``) is non-empty. Direct API v5 ``keywords.add``
    requires ``AdGroupId`` per keyword, and ``adgroups.update`` requires
    the target group ``Id`` to set shared group-level ``NegativeKeywords``.
    If a user tries to add a positive or negative keyword without an
    ``ad_group_id`` the request is rejected at endpoint validation with
    HTTP 400, BEFORE any package is built.
    """

    add_negative_keywords: list[str] | None = None
    add_keywords: list[str] | None = None
    # Target ad group for both the positive ``keywords.add`` and the
    # group-level ``adgroups.update`` ``NegativeKeywords`` write. Must
    # be present if either list is non-empty.
    ad_group_id: int | str | None = None
    # Free-form reason recorded in the audit log so we can later
    # reconstruct WHY a particular change was approved.
    reason: str | None = None


class SemanticChangeOperation(BaseModel):
    """One Direct API v5 operation that would be sent on apply.

    The shape mirrors the real ``keywords`` / ``adgroups`` v5 services
    closely enough that the ``preview`` field can be inspected manually
    before any live write. We never invent fields that the real API
    does not accept.
    """

    method: str  # e.g. "keywords.add" or "adgroups.update"
    params: dict


class SemanticChangePreview(BaseModel):
    operations: list[SemanticChangeOperation]
    # ``merge_on_apply`` is True when the live apply path will read
    # existing group-level negative keywords from Yandex and merge them
    # with the requested phrases before calling ``adgroups.update``.
    # Required because the Direct API v5 ``NegativeKeywords.Items`` field
    # on ``adgroups.update`` is REPLACE, not APPEND. The pure-local
    # ``prepare``/``dry_run`` paths never touch the network; this flag
    # is purely a heads-up that the live apply will perform a read-modify
    # write with de-duplication and order preservation.
    merge_on_apply: bool = False
    # Short human-readable semantics note describing the merge behaviour
    # so the user sees it in the prepare / dry-run response, not just in
    # internal audit details. Example value: "adgroups.update with
    # NegativeKeywords.Items is REPLACE on Direct; live apply will read
    # the current group-level negatives and merge with the requested
    # phrases, preserving order and de-duplicating."
    semantics_note: str | None = None


class SemanticChangePackage(BaseModel):
    package_id: str
    campaign_id: str
    status: Literal["prepared", "applied", "rejected"] = "prepared"
    mode: str
    dry_run: bool = True
    created_at: str
    reason: str | None = None
    preview: SemanticChangePreview
    audit_id: str | None = None


class SemanticChangeApplyRequest(BaseModel):
    """Body of ``POST /semantic-changes/{package_id}/apply``.

    Mirrors :class:`ApplyActionRequest` so the same gate contract
    (``dry_run / approved / idempotency_key``) is enforced consistently
    across the whole product surface.
    """

    dry_run: bool = True
    approved: bool
    idempotency_key: str = Field(..., min_length=6)


class SemanticChangeApplyResult(BaseModel):
    package_id: str
    campaign_id: str
    mode: str
    dry_run: bool
    applied: bool
    audit_id: str
    source: Literal["mock", "yandex"] = "mock"
    operations_sent: int = 0


# ---------------------------------------------------------------------------
# Live-create campaign (Yandex Direct API v5 `campaigns.add`).
#
# Gated by the standard product contract: live_write only, dry_run +
# approved + idempotency_key. live_readonly blocks real writes before
# any network call. Stage 1 covers the `TextCampaign` family only
# (`TEXT_CAMPAIGN` and `MOBILE_APP_CAMPAIGN` are out of scope here; they
# require additional, model-specific fields). The endpoint is the
# one-stop "create a real Yandex campaign from a draft" call, but the
# network write is intentionally split into stages so we never
# silently skip a failing step. The apply path will be extended with
# `adgroups.add`, `ads.add`, `keywords.add` in follow-up PRs; the
# scaffolding is already here so the audit log has stable field names.
# ---------------------------------------------------------------------------


class LiveCreateCampaignRequest(BaseModel):
    """Body of ``POST /yandex/campaigns/live-create``.

    The campaign is built from an existing :class:`CampaignDraft` so the
    draft preview stays the single source of truth for name, business
    type, region, monthly budget, landing URL and ad group / ad /
    keyword structure. Direct API v5 ``campaigns.add`` for a text
    campaign requires the following mandatory fields:

    * ``Name`` — campaign name
    * ``StartDate`` — ``YYYY-MM-DD`` (no future date required, but
      the v5 service rejects very far-future values)
    * ``TextCampaign`` block with at least one of:
      ``BiddingStrategy`` (``AVERAGE_CPC`` / ``AVERAGE_CPA`` / etc.),
      ``Settings`` (Geo / Time-targeting), or ``CounterIds``.

    The endpoint accepts an explicit ``counter_ids`` (for Metrika
    conversion attribution) and an explicit ``start_date`` override
    (``YYYY-MM-DD``). Both are optional — sensible defaults are filled
    in below.
    """

    draft_id: str = Field(..., min_length=1)
    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True
    start_date: str | None = Field(
        default=None,
        description="Optional YYYY-MM-DD override; defaults to today (UTC).",
    )
    counter_ids: list[int] = Field(
        default_factory=list,
        description="Optional Metrika counter ids for conversion attribution.",
    )
    utm_config: UtmConfig | None = Field(
        default=None,
        description=(
            "Optional UTM configuration for ad Hrefs at creation time. "
            "When enabled=True, every ad's Href is tagged with UTM params. "
            "If not provided here, the draft's stored utm_config is used."
        ),
    )
    reason: str | None = None


class LiveCreateCampaignResult(BaseModel):
    """Response envelope for live-create.

    ``dry_run=True`` returns the chained v5 payload preview (one
    entry per stage, plus the stage-1 ``campaigns.add`` envelope)
    that WOULD be sent, with ``applied=False`` and
    ``campaign_id=None``. ``dry_run=False`` (live_write only)
    returns the new ids from the Yandex ``AddResults`` envelope,
    with ``applied=True`` and ``source="yandex"``. Both paths record
    an audit event with the request id and the live stages that
    were actually executed.

    Live-create chains four Direct API v5 stages behind one
    ``POST /yandex/campaigns/live-create`` call:

    1. ``campaigns.add`` — text-campaign family; new campaign id.
    2. ``adgroups.add`` — for each draft ad group; local→Yandex
       ad group id map is built and used by stages 3 and 4. Each
       item ALWAYS carries ``RegionIds`` (v5 rejects items without
       a geo target); ids are resolved from ``draft.region`` via
       the explicit local map ``_REGION_NAME_TO_V5_IDS`` in
       ``app/store.py``. The ``NegativeKeywords`` block is OPTIONAL
       on v5 ``adgroups.add``: omitted when the draft has no
       negatives, included with the items when it does.
    3. ``ads.add`` — for each draft ad; ``AdGroupId`` is the
       Yandex id from stage 2.
    4. ``keywords.add`` — for each draft keyword (group-level and
       campaign-level); ``AdGroupId`` is the Yandex id from stage 2.

    Each stage is its own v5 call so a single failure stops the
    chain before the next stage. The chain does NOT call
    ``campaigns.resume`` automatically; Direct controls the initial
    lifecycle/moderation state. Activation goes through the existing
    ``POST /yandex/campaigns/{campaign_id}/resume`` endpoint with its
    own approval / idempotency gate.

    ``negativekeywordsharedsets.add`` is the only stage kept as
    ``not_implemented`` (the v5 shape is not documented in the
    project sources and the read-only path for shared sets is
    read-only by design). Group-level negatives are applied via
    the confirmed ``adgroups.add`` ``NegativeKeywords.Items``
    block on creation — the same shape the existing
    ``adgroups.update`` semantic-change path uses — so the
    negative-keyword requirement is satisfied without inventing a
    payload shape for an undocumented v5 service.

    An unknown / empty ``draft.region`` raises
    :class:`YandexDirectError` BEFORE any ``campaigns.add`` call
    (audit ``live_create_campaign_failed``); the endpoint returns
    HTTP 502 with a redacted message that names the offending
    region so the operator can fix it. The dry-run preview surfaces
    the same failure so the operator sees the same mode in both
    paths.
    """

    draft_id: str
    mode: str
    dry_run: bool
    applied: bool
    campaign_id: str | None = None
    source: Literal["mock", "yandex"] = "yandex"
    audit_id: str
    payload_preview: dict | None = None
    stages_executed: list[str] = Field(default_factory=list)
    ad_group_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Yandex ad group ids returned by adgroups.add, in the order "
            "the ad groups were submitted. Empty when stage 2 was "
            "skipped or failed."
        ),
    )
    ad_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Yandex ad ids returned by ads.add, in the order the ads "
            "were submitted. Empty when stage 3 was skipped or failed."
        ),
    )
    keyword_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Yandex keyword ids returned by keywords.add, in the order "
            "the keywords were submitted. Empty when stage 4 was "
            "skipped (no keywords on the draft) or failed."
        ),
    )
    not_implemented: list[str] = Field(
        default_factory=list,
        description=(
            "Stages not yet executed by the store-level live-create "
            "chain. Currently only ``negativekeywordsharedsets.add`` "
            "is kept as ``not_implemented`` (the v5 shape for that "
            "service is not documented in the project sources; group-"
            "level negatives are applied via the confirmed "
            "``adgroups.add`` / ``adgroups.update`` "
            "``NegativeKeywords.Items`` path instead). Activation is "
            "intentionally NOT performed automatically; Direct controls "
            "the initial lifecycle/moderation state. The operator can "
            "activate it via the existing "
            "``POST /yandex/campaigns/{campaign_id}/resume`` endpoint "
            "with its own approval and idempotency gate."
        ),
    )


# ---------------------------------------------------------------------------
# Time-targeting update (Yandex Direct API v5 ``campaigns.update``)
#
# A safe-by-default write endpoint for the campaign-level
# TimeTargeting / hourly-bidding schedule. Mirrors the same gate
# contract as the rest of the product surface: dry_run is always
# allowed and never mutates; live_readonly blocks real writes before
# any network call; live_write requires ``approved=true`` and an
# ``idempotency_key`` for the real apply.
#
# The v5 contract for ``TimeTargeting`` is documented at
# https://yandex.com/dev/direct/doc/ref-v5/campaigns/update.html
# and is the same shape that ``campaigns.get`` already returns
# under the ``TimeTargeting`` field. Direct v5 represents a weekly
# schedule as a list of seven ``TimeTargetItem`` blocks, one per day
# of the week (MONDAY..SUNDAY), each carrying 24 hourly
# ``BidPercent`` values (0..100). See ``YandexTimeTargetingSchedule``
# below for the day-of-week -> index mapping.
# ---------------------------------------------------------------------------


# 7 days, 24 hours per day, percentages 0..100 (Direct's
# ``BidPercent`` is the percentage of the ad group's base bid that
# applies during that hour; 100 = full bid, 0 = paused).
WEEK_DAY_NAMES: tuple[str, ...] = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
)
HOURS_PER_DAY: int = 24
DAYS_PER_WEEK: int = 7


class YandexTimeTargetingHourly(BaseModel):
    """A single day's 24-hour schedule.

    Direct v5 represents a TimeTargetItem's hourly ``BidPercent`` as
    a list of 24 integer percentages. The list is ordered by hour
    (index 0 = 00:00, index 23 = 23:00) and the value range is
    0..100 (``0`` pauses the campaign during that hour, ``100`` is
    the ad-group base bid).
    """

    hours: list[int] = Field(
        ...,
        min_length=HOURS_PER_DAY,
        max_length=HOURS_PER_DAY,
        description=(
            "24 hourly BidPercent values, 0..100. Index 0 = 00:00, "
            "index 23 = 23:00 (local campaign timezone)."
        ),
    )

    @field_validator("hours", mode="before")
    @classmethod
    def _no_bools_in_hours(cls, value: Any) -> Any:
        # Pydantic auto-coerces ``True`` -> ``1`` and ``False`` ->
        # ``0`` for ``list[int]``; a real bidder who typed
        # ``hours=[True, False, ...]`` would otherwise see
        # ``[1, 0, ...]`` silently. Block that here.
        if isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, bool):
                    raise ValueError(
                        f"hours[{index}] must be an integer 0..100, "
                        f"got bool: {item!r}"
                    )
        return value

    @field_validator("hours")
    @classmethod
    def _validate_percent_range(cls, value: list[int]) -> list[int]:
        for index, item in enumerate(value):
            if item < 0 or item > 100:
                raise ValueError(
                    f"hours[{index}]={item} is out of range 0..100"
                )
        return value


class YandexTimeTargetingSchedule(BaseModel):
    """A 7-day weekly schedule.

    The seven ``days`` entries are ALWAYS in the v5 day-of-week
    order MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY,
    SUNDAY. The model treats the list as positional: index 0 maps
    to MONDAY and index 6 maps to SUNDAY. It validates the length
    and hour vectors; it does not infer or re-order named days.

    Each day's 24 values follow the v5 contract documented on
    ``YandexTimeTargetingHourly`` above.
    """

    days: list[YandexTimeTargetingHourly] = Field(
        ...,
        min_length=DAYS_PER_WEEK,
        max_length=DAYS_PER_WEEK,
        description=(
            "Seven 24-hour schedule entries, one per day of the week. "
            "The list is positional in the v5 day-of-week order "
            "MONDAY..SUNDAY; no named-day re-ordering is inferred."
        ),
    )

    @model_validator(mode="after")
    def _validate_v5_day_order(self) -> "YandexTimeTargetingSchedule":
        # The schedule is positional: the first entry maps to
        # MONDAY, the second to TUESDAY, ... the seventh to
        # SUNDAY. The ``hours`` / ``days`` request shape is the
        # only entry path that does the expansion into the
        # canonical order; the ``schedule`` shape is positional
        # by definition. We re-validate the count here so direct
        # callers that bypass Pydantic still get a typed error.
        if len(self.days) != DAYS_PER_WEEK:
            # Pydantic's min_length / max_length has already
            # enforced this; the guard is here for direct callers
            # that bypass Pydantic.
            raise ValueError(
                f"schedule.days must have exactly {DAYS_PER_WEEK} entries"
            )
        return self


class YandexTimeTargetingRequest(BaseModel):
    """Body of ``POST /yandex/campaigns/{campaign_id}/time-targeting``.

    Accepts the schedule in one of two shapes:

    1. ``schedule`` — a :class:`YandexTimeTargetingSchedule` with the
       full 7 x 24 matrix. Always validated.
    2. ``hours`` — a flat 24-value list (0..100) plus an optional
       ``days`` filter (``["MONDAY", ..., "SUNDAY"]``). Convenience
       for the common "use these hours every day" use case; the
       endpoint expands it into the canonical 7 x 24 matrix.

    Either ``schedule`` or ``hours`` MUST be supplied; supplying
    both is a 400-level validation error.

    The standard product gate contract applies:
    ``approved`` / ``idempotency_key`` / ``dry_run`` plus the
    runtime mode (live_readonly blocks real writes before any
    network call; live_write requires ``approved=true`` and
    ``idempotency_key`` for the real apply).
    """

    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True
    schedule: YandexTimeTargetingSchedule | None = Field(
        default=None,
        description=(
            "Full 7 x 24 weekly schedule. Re-ordered to v5 day-of-"
            "week order on the way in. Mutually exclusive with "
            "``hours``."
        ),
    )
    hours: list[int] | None = Field(
        default=None,
        description=(
            "Flat 24-value hourly list (0..100). Used with "
            "``days`` to expand into a full 7 x 24 schedule. "
            "Mutually exclusive with ``schedule``."
        ),
    )
    days: list[str] | None = Field(
        default=None,
        description=(
            "Optional day filter for the ``hours`` shape. Defaults "
            "to all seven days when omitted. Ignored when "
            "``schedule`` is supplied."
        ),
    )
    timezone: str | None = Field(
        default=None,
        description=(
            "Optional human-readable timezone label, recorded in "
            "the audit and the readback so the operator can see "
            "which timezone the schedule applies to. Direct's "
            "``TimeTargeting`` itself does not carry a timezone "
            "field; this label is metadata only and is NEVER sent "
            "to the v5 service."
        ),
    )
    reason: str | None = None

    @field_validator("hours", mode="before")
    @classmethod
    def _no_bools_in_request_hours(cls, value: Any) -> Any:
        # Same guard as on ``YandexTimeTargetingHourly`` —
        # Pydantic auto-coerces ``True``/``False`` to ``1``/``0``
        # for ``list[int]``; we reject the silent coercion here.
        if isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, bool):
                    raise ValueError(
                        f"hours[{index}] must be an integer 0..100, "
                        f"got bool: {item!r}"
                    )
        return value

    @model_validator(mode="after")
    def _validate_either_or(self) -> "YandexTimeTargetingRequest":
        if (self.schedule is None) == (self.hours is None):
            raise ValueError(
                "exactly one of `schedule` or `hours` must be supplied"
            )
        if self.hours is not None:
            if len(self.hours) != HOURS_PER_DAY:
                raise ValueError(
                    f"`hours` must contain exactly {HOURS_PER_DAY} "
                    f"values, got {len(self.hours)}"
                )
            for index, value in enumerate(self.hours):
                if value < 0 or value > 100:
                    raise ValueError(
                        f"hours[{index}]={value} is out of range 0..100"
                    )
            if self.days is not None:
                seen: set[str] = set()
                for day in self.days:
                    normalized = day.strip().upper()
                    if normalized not in WEEK_DAY_NAMES:
                        raise ValueError(
                            f"days entry {day!r} is not one of "
                            f"{list(WEEK_DAY_NAMES)}"
                        )
                    if normalized in seen:
                        raise ValueError(
                            f"days entry {day!r} is duplicated"
                        )
                    seen.add(normalized)
        return self


class YandexTimeTargetingResult(BaseModel):
    """Response envelope for the time-targeting update endpoint.

    ``dry_run=True`` returns the v5 ``campaigns.update`` payload
    preview (one ``TimeTargeting`` block with seven ``TimeTargetItem``
    entries) that WOULD be sent, with ``applied=False``. ``dry_run=False``
    (live_write only) returns the read-back from ``campaigns.get``
    after the apply, with ``applied=True`` and ``source="yandex"``.

    The ``schedule_applied`` field is the canonical v5-shape matrix
    (7 x 24) the endpoint actually sent to the v5 service, returned
    unchanged on dry-run and on apply. The operator can diff it
    against the live readback (also returned) to confirm that the
    schedule was applied as expected.
    """

    campaign_id: str
    mode: str
    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "yandex"
    audit_id: str
    payload_preview: dict | None = Field(
        default=None,
        description=(
            "The v5 ``campaigns.update`` payload that WOULD be sent. "
            "Present on dry-run; ``None`` on a successful apply "
            "(the read-back fills the operator-facing schedule)."
        ),
    )
    schedule_applied: YandexTimeTargetingSchedule | None = Field(
        default=None,
        description=(
            "The canonical 7 x 24 schedule that was actually sent to "
            "(or, in dry-run, would be sent to) the v5 service. "
            "Always present."
        ),
    )
    readback: dict | None = Field(
        default=None,
        description=(
            "The ``TimeTargeting`` block returned by the post-apply "
            "``campaigns.get`` read-back. ``None`` on dry-run."
        ),
    )
    timezone: str | None = Field(
        default=None,
        description=(
            "Timezone label echoed from the request, recorded in "
            "the audit and returned to the operator. Direct's "
            "TimeTargeting does not carry a timezone; this field "
            "is metadata only and is never sent to v5."
        ),
    )


# ---------------------------------------------------------------------------
# TimeTargeting read-only GET response
# ---------------------------------------------------------------------------


class YandexTimeTargetingReadResult(BaseModel):
    """Response envelope for ``GET /yandex/campaigns/{campaign_id}/time-targeting``.

    Returns the current ``TimeTargeting`` block from Yandex Direct
    (v5 ``campaigns.get`` with ``TimeTargeting`` field). In mock
    mode returns a deterministic schedule. Always ``read_only=True``
    — no mutation, no write gate, no ``approved`` / ``idempotency_key``.
    """

    campaign_id: str
    campaign_name: str | None = Field(
        default=None,
        description="Campaign name from Yandex Direct (live mode) or mock.",
    )
    source: Literal["mock", "yandex"] = "yandex"
    read_only: bool = True
    time_targeting: dict | None = Field(
        default=None,
        description=(
            "Raw ``TimeTargeting`` block as returned by v5 "
            "``campaigns.get``. Contains ``Schedule.Items`` (7 strings), "
            "``ConsiderWorkingWeekends``, and ``HolidaysSchedule``. "
            "``None`` when the read fails or Yandex returns no block."
        ),
    )
    schedule: YandexTimeTargetingSchedule | None = Field(
        default=None,
        description=(
            "Normalized 7 x 24 schedule representation useful for "
            "human readers. Built from the raw ``TimeTargeting`` when "
            "available; ``None`` when the raw block cannot be parsed."
        ),
    )


# ---------------------------------------------------------------------------
# Campaign strategy read / update
# ---------------------------------------------------------------------------


class YandexStrategyReadResult(BaseModel):
    """Response envelope for ``GET /yandex/campaigns/{campaign_id}/strategy``.

    Read-only in all modes — no ``approved``, no ``idempotency_key``,
    no network write call. Returns the current campaign type, strategy
    block, CounterIds (if available), DailyBudget (if relevant),
    State, Status, Name, and PriorityGoals (if any) from
    Yandex Direct v5 ``campaigns.get``.
    """

    campaign_id: str
    campaign_name: str | None = Field(
        default=None,
        description="Campaign name from Yandex Direct (live) or mock.",
    )
    source: Literal["mock", "yandex"] = "yandex"
    read_only: bool = True
    campaign_type: str | None = Field(
        default=None,
        description="Campaign Type from Direct v5 (e.g. TEXT_CAMPAIGN).",
    )
    state: str | None = Field(
        default=None,
        description="Campaign State from Direct v5 (e.g. ON, OFF).",
    )
    status: str | None = Field(
        default=None,
        description="Campaign Status from Direct v5 (e.g. DRAFT, ACCEPTED).",
    )
    daily_budget: dict | None = Field(
        default=None,
        description=(
            "Current DailyBudget block (Amount in micros, SpendMode). "
            "``null``/None when the campaign has no daily budget configured "
            "(e.g. after switching to a weekly conversion strategy)."
        ),
    )
    counter_ids: list[int] | None = Field(
        default=None,
        description="CounterIds attached to the campaign in Direct v5, if any.",
    )
    priority_goals: dict | None = Field(
        default=None,
        description=(
            "Raw ``TextCampaign.PriorityGoals`` block as returned by v5 "
            "``campaigns.get``. Contains ``Items`` list with ``GoalId`` "
            "and ``Value`` (in Direct micros). Only present for multi-goal "
            "WB_MAXIMUM_CONVERSION_RATE (GoalId=13)."
        ),
    )
    strategy: dict | None = Field(
        default=None,
        description=(
            "Raw TextCampaign.BiddingStrategy block as returned by v5 "
            "``campaigns.get``. Contains ``Search`` and ``Network`` "
            "sub-objects with strategy-type-specific params."
        ),
    )
    strategy_summary: dict | None = Field(
        default=None,
        description=(
            "Normalized human-readable strategy summary. "
            "Example: {search: {type: 'WB_MAXIMUM_CONVERSION_RATE', "
            "goal_id: 567732835, weekly_spend_limit_rub: 7000.0, "
            "bid_ceiling_rub: 1500.0}, network: {type: 'SERVING_OFF'}, "
            "priority_goals: [{goal_id: 1, value_rub: 10.0}, ...]}."
        ),
    )


MULTI_GOAL_STRATEGY_ID = 13
"""GoalId used in WbMaximumConversionRate when PriorityGoals are present.

Yandex Direct changelog mentions GoalId=13 for priority-goal-based
WB_MAXIMUM_CONVERSION_RATE on TEXT_CAMPAIGN.  When the caller provides
``goal_ids`` or ``priority_goals``, the store sets ``GoalId=13`` and
populates ``PriorityGoals.Items`` in the TextCampaign block.
"""

GOAL_IDS_MAX = 30
"""Direct API maximum for PriorityGoals.Items."""


class PriorityGoal(BaseModel):
    """Single priority goal with optional conversion value.

    ``value`` is in RUBLES (public REST convention).  The store
    converts to Direct micros (× 1 000 000).  When ``value`` is
    ``None`` or omitted, a default of 1.0 RUB is used (equal-weight
    multi-goal convenience path).
    """

    goal_id: int = Field(..., ge=1, description="Metrika goal id.")
    value: float | None = Field(
        default=None,
        gt=0,
        description="Conversion value in RUBLES. Defaults to 1.0 when omitted.",
    )


class YandexStrategyRequest(BaseModel):
    """Body of ``POST /yandex/campaigns/{campaign_id}/strategy``.

    Updates the search TextCampaign.BiddingStrategy for an existing
    campaign. Currently supports switching the search channel to
    ``WB_MAXIMUM_CONVERSION_RATE``. The standard product gate contract
    applies: ``dry_run=True`` is preview-only (default); real apply
    requires ``DIRECTPILOT_MODE=live_write``, ``approved=True``,
    ``idempotency_key``, and ``dry_run=False``.

    The caller MUST choose exactly ONE goal-selection mode:

    * **Single goal** (backward-compatible): set ``goal_id``.
    * **Multi-goal equal weight**: set ``goal_ids`` (list of goal ids,
      default conversion value 1.0 RUB each).
    * **Multi-goal explicit values**: set ``priority_goals`` (list of
      ``{goal_id, value}`` objects with values in RUBLES).

    ``weekly_spend_limit`` and ``bid_ceiling`` are in RUBLES
    (public REST convention). The store converts to Direct micros
    (multiply by 1_000_000) before building the v5 payload. The
    conversion is exact and documented.
    """

    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True
    strategy_type: Literal["WB_MAXIMUM_CONVERSION_RATE"] = Field(
        default="WB_MAXIMUM_CONVERSION_RATE",
        description="Search bidding strategy type to set.",
    )
    goal_id: int | None = Field(
        default=None,
        ge=1,
        description="Metrika goal id for WB_MAXIMUM_CONVERSION_RATE (single-goal mode).",
    )
    goal_ids: list[int] | None = Field(
        default=None,
        description=(
            "Multiple Metrika goal ids for equal-weight multi-goal optimization. "
            "Max 30 items, unique, positive. Sets ``GoalId=13`` on the strategy "
            "and populates ``PriorityGoals.Items`` with equal 1.0 RUB values."
        ),
    )
    priority_goals: list[PriorityGoal] | None = Field(
        default=None,
        description=(
            "Explicit priority goals with per-goal conversion values in RUBLES. "
            "Max 30 items, unique positive goal ids. Sets ``GoalId=13`` on the "
            "strategy and populates ``PriorityGoals.Items``."
        ),
    )
    weekly_spend_limit: float = Field(
        ...,
        gt=0,
        description="Weekly spend limit in RUBLES. Converted to micros for Direct.",
    )
    bid_ceiling: float | None = Field(
        default=None,
        gt=0,
        description="Optional bid ceiling in RUBLES. Converted to micros for Direct.",
    )
    network: Literal["SERVING_OFF"] | None = Field(
        default=None,
        description=(
            "Explicit Network strategy override. When omitted, the current "
            "Network strategy is preserved from readback. Set to "
            "``SERVING_OFF`` to explicitly disable networks. The endpoint "
            "never silently turns networks ON."
        ),
    )
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_goal_mode(self) -> "YandexStrategyRequest":
        """Ensure exactly one goal-selection mode is used.

        Modes are mutually exclusive:
        ``goal_id`` OR ``goal_ids`` OR ``priority_goals``.
        """
        modes = [
            self.goal_id is not None,
            self.goal_ids is not None,
            self.priority_goals is not None,
        ]
        active = sum(modes)
        if active == 0:
            raise ValueError(
                "One of goal_id, goal_ids, or priority_goals is required"
            )
        if active > 1:
            raise ValueError(
                "goal_id, goal_ids, and priority_goals are mutually exclusive"
            )
        # Validate goal_ids list
        if self.goal_ids is not None:
            if len(self.goal_ids) == 0:
                raise ValueError("goal_ids must not be empty")
            if len(self.goal_ids) > GOAL_IDS_MAX:
                raise ValueError(
                    f"goal_ids: max {GOAL_IDS_MAX} items, got {len(self.goal_ids)}"
                )
            if len(set(self.goal_ids)) != len(self.goal_ids):
                raise ValueError("goal_ids contains duplicates")
            if any(g < 1 for g in self.goal_ids):
                raise ValueError("goal_ids must contain positive integers only")
        # Validate priority_goals list
        if self.priority_goals is not None:
            if len(self.priority_goals) == 0:
                raise ValueError("priority_goals must not be empty")
            if len(self.priority_goals) > GOAL_IDS_MAX:
                raise ValueError(
                    f"priority_goals: max {GOAL_IDS_MAX} items, "
                    f"got {len(self.priority_goals)}"
                )
            gids = [pg.goal_id for pg in self.priority_goals]
            if len(set(gids)) != len(gids):
                raise ValueError("priority_goals contains duplicate goal_id values")
        return self


class YandexStrategyResult(BaseModel):
    """Response envelope for ``POST /yandex/campaigns/{campaign_id}/strategy``.

    * ``dry_run=True`` returns ``applied=False`` with ``payload_preview``.
    * ``dry_run=False`` + ``live_write`` returns ``applied=True``
      with optional ``readback``.
    * ``source=\"mock\"`` only in mock-mode dry-run.
    """

    campaign_id: str
    mode: str
    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "yandex"
    audit_id: str
    payload_preview: dict | None = Field(
        default=None,
        description=(
            "The v5 ``campaigns.update`` payload that WOULD be sent. "
            "Present on dry-run; ``None`` on a successful apply."
        ),
    )
    strategy_applied: dict | None = Field(
        default=None,
        description="The strategy configuration that was applied (normalized).",
    )
    readback: dict | None = Field(
        default=None,
        description="Post-apply readback of the strategy block from Direct. ``None`` on dry-run.",
    )
    provider_warnings: list["ProviderWarning"] = Field(default_factory=list)
    yandex_units: int | None = None
    yandex_error: str | None = None


# ---------------------------------------------------------------------------
# Autotargeting settings read / update
# ---------------------------------------------------------------------------


AUTOTARGETING_CATEGORIES = ("Exact", "Narrow", "Alternative", "Accessory", "Broader")
"""Ordered autotargeting category names as documented by Direct API v5.

Used for building deterministic ``AutotargetingSettings.Categories``
payloads where every category must be explicitly ``YES`` or ``NO``.
"""

AUTOTARGETING_BRAND_OPTIONS = ("WithoutBrands", "WithAdvertiserBrand", "WithCompetitorsBrand")
"""Ordered brand-option names as documented by Direct API v5."""

_AUTOTARGETING_CATEGORY_PRESETS: dict[str, dict[str, str]] = {
    "exact_narrow": {
        "Exact": "YES",
        "Narrow": "YES",
        "Alternative": "NO",
        "Accessory": "NO",
        "Broader": "NO",
    },
    "exact_narrow_broader": {
        "Exact": "YES",
        "Narrow": "YES",
        "Alternative": "NO",
        "Accessory": "NO",
        "Broader": "YES",
    },
}
"""Safe autotargeting category presets.

* ``exact_narrow`` — default for local service-search campaigns.
* ``exact_narrow_broader`` — optional wider reach when explicitly approved.
"""

_AUTOTARGETING_BRAND_PRESETS: dict[str, dict[str, str]] = {
    "own_no_competitors": {
        "WithoutBrands": "YES",
        "WithAdvertiserBrand": "YES",
        "WithCompetitorsBrand": "NO",
    },
}
"""Safe autotargeting brand-option presets.

* ``own_no_competitors`` — own-brand + no-brand yes, competitors no (default).
"""


class AutotargetingCategories(BaseModel):
    """Yandex Direct v5 ``AutotargetingSettings.Categories`` block.

    Every category must be explicitly ``YES`` or ``NO`` to avoid the
    Direct API pitfall where missing categories silently default to
    ``YES`` (all categories enabled).
    """

    exact: str = Field(default="YES", pattern="^(YES|NO)$")
    narrow: str = Field(default="YES", pattern="^(YES|NO)$")
    alternative: str = Field(default="NO", pattern="^(YES|NO)$")
    accessory: str = Field(default="NO", pattern="^(YES|NO)$")
    broader: str = Field(default="NO", pattern="^(YES|NO)$")

    def to_direct_dict(self) -> dict[str, str]:
        return {
            "Exact": self.exact,
            "Narrow": self.narrow,
            "Alternative": self.alternative,
            "Accessory": self.accessory,
            "Broader": self.broader,
        }


class AutotargetingBrandOptions(BaseModel):
    """Yandex Direct v5 ``AutotargetingSettings.BrandOptions`` block."""

    without_brands: str = Field(default="YES", pattern="^(YES|NO)$")
    with_advertiser_brand: str = Field(default="YES", pattern="^(YES|NO)$")
    with_competitors_brand: str = Field(default="NO", pattern="^(YES|NO)$")

    def to_direct_dict(self) -> dict[str, str]:
        return {
            "WithoutBrands": self.without_brands,
            "WithAdvertiserBrand": self.with_advertiser_brand,
            "WithCompetitorsBrand": self.with_competitors_brand,
        }


class YandexAutotargetingReadItem(BaseModel):
    """One ad group's autotargeting row from ``keywords.get``."""

    ad_group_id: str
    ad_group_name: str
    autotargeting_keyword_id: str
    status: str
    state: str | None = None
    serving_status: str | None = None
    categories: dict[str, str] | None = None
    brand_options: dict[str, str] | None = None
    raw_provider: dict | None = Field(
        default=None,
        description=(
            "Raw keyword row from Direct API v5 keywords.get. "
            "Redacted in public responses — never contains tokens. "
            "Provided for debugging/support."
        ),
    )


class YandexAutotargetingReadResult(BaseModel):
    """Response envelope for ``GET /yandex/campaigns/{campaign_id}/autotargeting``."""

    campaign_id: str
    source: Literal["mock", "yandex"] = "yandex"
    read_only: bool = True
    ad_groups: list[YandexAutotargetingReadItem] = Field(default_factory=list)
    default_preset: str = "exact_narrow"


class YandexAutotargetingRequest(BaseModel):
    """Body of ``POST /yandex/campaigns/{campaign_id}/autotargeting``.

    Standard product gate contract applies:
    ``dry_run=True`` (default) is preview-only;
    ``dry_run=False`` requires ``DIRECTPILOT_MODE=live_write``,
    ``approved=True`` and a valid ``idempotency_key``.

    One of ``preset``, ``categories``, or ``ad_group_overrides``
    determines the autotargeting settings to apply. When a
    ``preset`` is used it defines the fallback categories and
    brand options.
    """

    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    dry_run: bool = True

    create_missing: bool = Field(
        default=False,
        description=(
            "If ``True``, create new ``---autotargeting`` rows for ad groups "
            "that lack one via ``keywords.add``. Default ``False`` means the "
            "endpoint will skip or report ad groups without autotargeting rows "
            "in ``skipped_ad_group_ids``.  ``create_missing=True`` is gated by "
            "the same ``dry_run`` / ``live_write`` / ``approved`` / "
            "``idempotency_key`` contract: dry-run previews the add payload "
            "alongside the update payload; apply dispatches real writes."
        ),
    )

    preset: Literal["exact_narrow", "exact_narrow_broader", "custom"] = Field(
        default="exact_narrow",
        description=(
            "Named autotargeting preset. ``exact_narrow`` is the safe default "
            "for local service-search campaigns. ``exact_narrow_broader`` adds "
            "Broader category. ``custom`` means the caller provides explicit "
            "``categories`` and/or ``brand_options``."
        ),
    )

    ad_group_ids: list[str] | None = Field(
        default=None,
        description=(
            "List of ad group ids to target. Omit to target ALL ad groups "
            "in the campaign that have autotargeting rows."
        ),
    )

    categories: AutotargetingCategories | None = Field(
        default=None,
        description=(
            "Explicit category configuration. Required only when "
            "``preset=custom``. Every category must be explicitly "
            "``YES`` or ``NO`` — missing categories are NOT defaulted "
            "to ``NO`` by this model."
        ),
    )

    brand_options: AutotargetingBrandOptions | None = Field(
        default=None,
        description=(
            "Explicit brand-option configuration. "
            "Defaults to own-brand + no-brand yes, competitors no "
            "when omitted."
        ),
    )

    reason: str | None = None

    @model_validator(mode="after")
    def _validate_custom_preset_has_categories(self) -> "YandexAutotargetingRequest":
        if self.preset == "custom" and self.categories is None:
            raise ValueError(
                "categories is required when preset=custom; "
                "otherwise use a named preset like exact_narrow or exact_narrow_broader"
            )
        return self

    @model_validator(mode="after")
    def _validate_ad_group_ids_non_empty(self) -> "YandexAutotargetingRequest":
        if self.ad_group_ids is not None and len(self.ad_group_ids) == 0:
            raise ValueError("ad_group_ids must not be empty when provided")
        return self

    def resolve_categories(self) -> dict[str, str]:
        """Return the effective categories dict."""
        if self.preset != "custom":
            return dict(_AUTOTARGETING_CATEGORY_PRESETS[self.preset])
        assert self.categories is not None  # guarded by _validate_custom_preset_has_categories
        return self.categories.to_direct_dict()

    def resolve_brand_options(self) -> dict[str, str]:
        """Return the effective brand-options dict."""
        if self.brand_options is not None:
            return self.brand_options.to_direct_dict()
        return dict(_AUTOTARGETING_BRAND_PRESETS["own_no_competitors"])


class YandexAutotargetingResult(BaseModel):
    """Response envelope for ``POST /yandex/campaigns/{campaign_id}/autotargeting``.

    * ``dry_run=True`` returns ``applied=False`` with ``payload_preview``
      (exact ``keywords.update`` payload that WOULD be sent).
    * ``dry_run=False`` + ``live_write`` returns ``applied=True``.
    """

    campaign_id: str
    mode: str
    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "yandex"
    audit_id: str

    preset: str | None = None
    categories_applied: dict[str, str] | None = None
    brand_options_applied: dict[str, str] | None = None

    targeted_ad_group_ids: list[str] = Field(default_factory=list)
    skipped_ad_group_ids: list[str] = Field(
        default_factory=list,
        description="Ad groups without autotargeting rows (skipped).",
    )
    updated_keyword_ids: list[str] = Field(default_factory=list)
    created_keyword_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Yandex keyword ids returned by ``keywords.add`` for newly-created "
            "``---autotargeting`` rows. Present only when ``create_missing=True`` "
            "was used."
        ),
    )

    payload_preview: dict | None = Field(
        default=None,
        description=(
            "The v5 ``keywords.update`` payload that WOULD be sent. "
            "Present on dry-run; ``None`` on a successful live apply."
        ),
    )
    provider_warnings: list["ProviderWarning"] = Field(default_factory=list)
    yandex_units: int | None = None
    yandex_error: str | None = None


# ---------------------------------------------------------------------------
# Keyword bids update — live-safe endpoint for SearchBid / ContextBid
# ---------------------------------------------------------------------------

_MICROS_PER_RUBLE: int = 1_000_000


class KeywordBidItem(BaseModel):
    """A single keyword bid update item in a batch request.

    At minimum ``keyword_id`` + ``search_bid_rub`` and/or
    ``context_bid_rub`` must be supplied.

    ``search_bid_rub`` / ``context_bid_rub`` are in RUBLES at the
    REST boundary — the store converts to Direct micros.

    ``autotargeting_search_bid_is_auto`` is optional; when the item
    targets an autotargeting row and the caller sets a manual
    ``search_bid_rub``, DirectPilot adds
    ``AutotargetingSearchBidIsAuto=\"NO\"`` by default. Set it to
    ``True`` explicitly to keep auto mode.
    """

    keyword_id: int = Field(..., ge=1, description="Yandex Direct KeywordId")
    search_bid_rub: float | None = Field(
        default=None,
        ge=0.0,
        description="Search bid in RUBLES. Converted to micros internally.",
    )
    context_bid_rub: float | None = Field(
        default=None,
        ge=0.0,
        description="Context bid in RUBLES. Converted to micros internally.",
    )
    autotargeting_search_bid_is_auto: bool | None = Field(
        default=None,
        description=(
            "If the item targets an autotargeting row and this is unset, "
            "``AutotargetingSearchBidIsAuto=\"NO\"`` is added when "
            "``search_bid_rub`` is supplied. Set to ``True`` to keep auto mode."
        ),
    )

    @model_validator(mode="after")
    def _validate_at_least_one_bid(self) -> "KeywordBidItem":
        if self.search_bid_rub is None and self.context_bid_rub is None:
            raise ValueError(
                "At least one of search_bid_rub or context_bid_rub is required"
            )
        return self

    def to_direct_micros_item(self) -> dict:
        """Build the minimal v5 ``KeywordBids`` item for this keyword."""
        item: dict = {"KeywordId": self.keyword_id}
        if self.search_bid_rub is not None:
            item["SearchBid"] = int(round(self.search_bid_rub * _MICROS_PER_RUBLE))
            # When setting manual search bid on an autotargeting row,
            # include AutotargetingSearchBidIsAuto="NO" unless
            # explicitly opted out.
            if self.autotargeting_search_bid_is_auto is not True:
                item["AutotargetingSearchBidIsAuto"] = "NO"
        if self.context_bid_rub is not None:
            item["ContextBid"] = int(round(self.context_bid_rub * _MICROS_PER_RUBLE))
        return item


class KeywordBidUpdateRequest(BaseModel):
    """Body of ``POST /yandex/campaigns/{campaign_id}/bids``.

    Standard product gate contract:
    ``dry_run=True`` (default) is preview-only — no external write.
    ``dry_run=False`` requires ``DIRECTPILOT_MODE=live_write``,
    ``approved=True`` and a valid ``idempotency_key``.

    ``items`` is a non-empty list of per-keyword bid changes.
    """

    dry_run: bool = True
    approved: bool
    idempotency_key: str = Field(..., min_length=6)
    items: list[KeywordBidItem] = Field(..., min_length=1, max_length=500)
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_items_unique_keyword_ids(self) -> "KeywordBidUpdateRequest":
        seen: set[int] = set()
        for item in self.items:
            if item.keyword_id in seen:
                raise ValueError(
                    f"Duplicate keyword_id {item.keyword_id} in items; "
                    f"each keyword_id must appear at most once per request"
                )
            seen.add(item.keyword_id)
        return self


class KeywordBidSetItemResult(BaseModel):
    """Per-item outcome from v5 ``keywordbids.set`` ``SetResults``.

    Each ``SetResults`` entry carries the keyword id and optional
    ``Errors`` / ``Warnings`` arrays.  This model surfaces those
    per-item outcomes in a redacted form — only ``code``, ``message``,
    and ``details`` per warning/error; the raw v5 envelope is never
    included.

    * ``has_errors=True`` means this item was **not** applied —
      the overall ``applied`` flag will be ``False`` when any item
      has errors.
    * ``has_warnings=True`` means the item was applied but Direct
      returned non-fatal warnings (e.g. 10160 — ставка не будет
      применена при auto-стратегии).
    """

    keyword_id: int
    has_errors: bool = False
    has_warnings: bool = False
    errors: list["ProviderWarning"] = Field(default_factory=list)
    warnings: list["ProviderWarning"] = Field(default_factory=list)


class KeywordBidUpdateResult(BaseModel):
    """Response for ``POST /yandex/campaigns/{campaign_id}/bids``.

    * ``dry_run=True`` returns ``applied=False`` with ``payload_preview``
      (the exact v5 ``keywordbids.set`` payload that WOULD be sent).
    * ``dry_run=False`` + ``live_write`` returns ``applied=True``
      with ``readback``.
    * ``provider_warnings`` surfaces upstream Direct warnings
      (e.g. 10160 — ставка не будет применена при auto-стратегии).
    * ``set_results`` surfaces per-item outcomes from the v5
      ``SetResults`` envelope (populated on live apply only).
      If any item in ``set_results`` has ``has_errors=True``,
      ``applied`` is ``False`` and ``partial_failure`` is ``True``.
    """

    campaign_id: str
    mode: str
    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "yandex"
    read_only: bool = False
    audit_id: str

    payload_preview: dict | None = Field(
        default=None,
        description=(
            "The v5 ``keywordbids.set`` payload that WOULD be sent. "
            "Present on dry_run; ``None`` on a successful live apply."
        ),
    )
    readback: list[dict] | None = Field(
        default=None,
        description=(
            "Current keyword bids read back after apply. "
            "Only the changed keyword ids with Bid/ContextBid."
        ),
    )
    provider_warnings: list["ProviderWarning"] = Field(default_factory=list)
    set_results: list["KeywordBidSetItemResult"] | None = Field(
        default=None,
        description=(
            "Per-item outcomes from the v5 ``SetResults`` envelope. "
            "Populated on live apply (``source=\"yandex\"``, ``dry_run=False``). "
            "Each item carries ``keyword_id``, ``has_errors``, ``has_warnings``, "
            "and redacted ``errors`` / ``warnings`` arrays."
        ),
    )
    partial_failure: bool = Field(
        default=False,
        description=(
            "``True`` when the top-level v5 call succeeded (``ok=true``) "
            "but one or more items in ``SetResults`` carry ``Errors``. "
            "In this case ``applied`` is ``False`` and ``set_results`` "
            "details which items failed."
        ),
    )
    not_implemented: list[str] = Field(default_factory=list)
    yandex_units: int | None = None
    yandex_error: str | None = None


# ---------------------------------------------------------------------------
# KeywordBids.get / keywordbids.setAuto
# ---------------------------------------------------------------------------


def _rubles_to_direct_micros(value: float) -> int:
    """Convert a public RUB value to an integral Direct micro value.

    Converting through ``str`` keeps the REST boundary deterministic instead
    of inheriting binary-float representation artefacts (for example 0.3).
    """

    return int(
        (Decimal(str(value)) * Decimal(_MICROS_PER_RUBLE)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


class KeywordBidsAuctionBidItem(BaseModel):
    traffic_volume: int | None = None
    bid_micros: int | None = None
    bid_rub: float | None = None
    price_micros: int | None = None
    price_rub: float | None = None


class KeywordBidsCoverageItem(BaseModel):
    probability: int | None = None
    bid_micros: int | None = None
    bid_rub: float | None = None


class KeywordBidsGetItem(BaseModel):
    """Typed, strategy-safe projection of one ``keywordbids.get`` row."""

    campaign_id: int
    ad_group_id: int | None = None
    keyword_id: int
    row_kind: Literal["keyword", "autotargeting", "unknown"]
    keyword: str | None = None
    serving_status: str | None = None
    strategy_priority: str | None = None
    search_bid_micros: int | None = None
    search_bid_rub: float | None = None
    search_autotargeting_is_auto: bool | None = None
    auction_bids: list[KeywordBidsAuctionBidItem] = Field(default_factory=list)
    network_bid_micros: int | None = None
    network_bid_rub: float | None = None
    coverage: list[KeywordBidsCoverageItem] = Field(default_factory=list)


class KeywordBidsGetResult(BaseModel):
    """Read-only canonical response for ``GET .../keyword-bids``."""

    campaign_id: str
    source: Literal["mock", "yandex"] = "yandex"
    read_only: bool = True
    items: list[KeywordBidsGetItem] = Field(default_factory=list)
    limited_by: int | None = None
    next_offset: int | None = None
    warnings: list[ProviderWarning] = Field(default_factory=list)


class SearchByTrafficVolumeRule(BaseModel):
    type: Literal["search_by_traffic_volume"] = "search_by_traffic_volume"
    target_traffic_volume: int = Field(..., ge=5, le=100)
    increase_percent: int = Field(default=0, ge=0, le=1000)
    bid_ceiling_rub: float = Field(..., gt=0)

    def to_direct_rule(self) -> dict:
        return {
            "SearchByTrafficVolume": {
                "TargetTrafficVolume": self.target_traffic_volume,
                "IncreasePercent": self.increase_percent,
                "BidCeiling": _rubles_to_direct_micros(self.bid_ceiling_rub),
            }
        }


class NetworkByCoverageRule(BaseModel):
    type: Literal["network_by_coverage"] = "network_by_coverage"
    target_coverage: int = Field(..., ge=0, le=100)
    increase_percent: int = Field(default=0, ge=0, le=1000)
    bid_ceiling_rub: float = Field(..., gt=0)

    def to_direct_rule(self) -> dict:
        return {
            "NetworkByCoverage": {
                "TargetCoverage": self.target_coverage,
                "IncreasePercent": self.increase_percent,
                "BidCeiling": _rubles_to_direct_micros(self.bid_ceiling_rub),
            }
        }


class KeywordBidsSetAutoRequest(BaseModel):
    """Strict preview/apply input for ``keywordbids.setAuto``.

    Scope is intentionally homogeneous: campaign scope uses the route campaign,
    while ad-group and keyword scopes require one non-empty, unique id list.
    """

    scope: Literal["campaign", "ad_group", "keyword"]
    ad_group_ids: list[int] | None = Field(default=None, max_length=1000)
    keyword_ids: list[int] | None = Field(default=None, max_length=10000)
    rule: SearchByTrafficVolumeRule | NetworkByCoverageRule = Field(discriminator="type")
    dry_run: bool = True
    approved: bool = False
    idempotency_key: str | None = Field(default=None, min_length=6)
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_scope_and_apply_key(self) -> "KeywordBidsSetAutoRequest":
        ad_group_ids = self.ad_group_ids or []
        keyword_ids = self.keyword_ids or []
        if self.scope == "campaign":
            if ad_group_ids or keyword_ids:
                raise ValueError("campaign scope must not include ad_group_ids or keyword_ids")
        elif self.scope == "ad_group":
            if not ad_group_ids or keyword_ids:
                raise ValueError("ad_group scope requires only non-empty ad_group_ids")
            if len(set(ad_group_ids)) != len(ad_group_ids):
                raise ValueError("ad_group_ids must be unique")
        elif self.scope == "keyword":
            if not keyword_ids or ad_group_ids:
                raise ValueError("keyword scope requires only non-empty keyword_ids")
            if len(set(keyword_ids)) != len(keyword_ids):
                raise ValueError("keyword_ids must be unique")
        if not self.dry_run and not self.idempotency_key:
            raise ValueError("idempotency_key is required when dry_run=false")
        return self

    @property
    def rule_type(self) -> Literal["search_by_traffic_volume", "network_by_coverage"]:
        return self.rule.type

    def build_direct_payload(self, campaign_id: int | str) -> dict:
        """Build the exact, strongly controlled v5 ``setAuto`` payload."""

        direct_campaign_id = int(campaign_id) if str(campaign_id).isdigit() else campaign_id
        if self.scope == "campaign":
            targets = [{"CampaignId": direct_campaign_id}]
        elif self.scope == "ad_group":
            targets = [{"AdGroupId": item} for item in self.ad_group_ids or []]
        else:
            targets = [{"KeywordId": item} for item in self.keyword_ids or []]
        bidding_rule = self.rule.to_direct_rule()
        return {
            "method": "setAuto",
            "params": {
                "KeywordBids": [
                    {**target, "BiddingRule": bidding_rule} for target in targets
                ]
            },
        }


class KeywordBidsSetAutoItemResult(BaseModel):
    campaign_id: int | None = None
    ad_group_id: int | None = None
    keyword_id: int | None = None
    has_errors: bool = False
    has_warnings: bool = False
    errors: list[ProviderWarning] = Field(default_factory=list)
    warnings: list[ProviderWarning] = Field(default_factory=list)


class KeywordBidsSetAutoResult(BaseModel):
    """Typed outcome for preview, blocked request, or gated ``setAuto`` apply."""

    campaign_id: str
    mode: str
    dry_run: bool
    applied: bool
    blocked: bool = False
    source: Literal["mock", "yandex"] = "yandex"
    read_only: bool = False
    audit_id: str
    payload_preview: dict | None = None
    affected_items: list[KeywordBidsGetItem] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    warnings: list[ProviderWarning] = Field(default_factory=list)
    set_auto_results: list[KeywordBidsSetAutoItemResult] | None = None
    partial_failure: bool = False
    readback: KeywordBidsGetResult | None = None
    readback_failed: bool = False
    verification_error: str | None = None
    yandex_units: int | None = None
    yandex_error: str | None = None


# ---------------------------------------------------------------------------
# Bid modifiers update — semantic preview + Direct v5 set payload
# ---------------------------------------------------------------------------


class YandexBidModifierItem(BaseModel):
    """Normalized read-only view of one Direct ``bidmodifiers.get`` item.

    Direct can return many modifier families (demographic, mobile, retargeting,
    weather, regional, video, etc.). DirectPilot keeps the original provider
    item for operator diagnostics and extracts common fields without inventing
    create/update payload shapes for modifier-specific conditions.
    """

    id: int | None = None
    campaign_id: int | None = None
    type: str = "UNKNOWN"
    bid_modifier: int | None = None
    adjustment_percent: int | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class YandexBidModifiersReadResult(BaseModel):
    """Read-only campaign bid-modifier list.

    The endpoint is intentionally read-only. Weather and other condition
    modifiers are represented when Direct returns them; writes update existing
    modifier ids only through the gated POST endpoint.
    """

    campaign_id: str
    source: Literal["mock", "yandex"] = "mock"
    read_only: bool = True
    items: list[YandexBidModifierItem] = Field(default_factory=list)
    raw: dict[str, Any] | None = None


class BidModifierAdjustment(BaseModel):
    """Generic existing bid-modifier adjustment for safe preview/apply.

    ``bidmodifiers.set`` updates an existing modifier by ``Id`` and
    ``BidModifier``. DirectPilot supports any existing modifier type (including
    weather modifiers returned by readback) by requiring the operator to provide
    the existing ``modifier_id`` for live apply. Modifier-specific condition
    metadata is preview-only and is never sent to ``bidmodifiers.set``.
    """

    modifier_id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Existing Yandex Direct bid modifier Id. Required for live apply; "
            "dry-run previews may omit it until readback resolves the modifier."
        ),
    )
    type_hint: str | None = Field(
        default=None,
        description="Operator-facing modifier type hint, e.g. Demographics, Weather, Mobile.",
    )
    age_range: Literal["AGE_0_17"] | None = Field(
        default=None,
        description="Backward-compatible under-18 demographic segment preview hint.",
    )
    adjustment_percent: int | None = Field(
        default=None,
        ge=-100,
        le=1200,
        description=(
            "Human-facing adjustment percent. Direct v5 uses BidModifier as a "
            "coefficient in percent, so this value is converted with "
            "BidModifier = 100 + adjustment_percent."
        ),
    )
    bid_modifier: int | None = Field(
        default=None,
        ge=0,
        le=1300,
        description="Direct v5 BidModifier coefficient. Alternative to adjustment_percent.",
    )
    conditions: dict[str, Any] = Field(
        default_factory=dict,
        description="Preview-only modifier-specific metadata, e.g. weather condition labels.",
    )

    @model_validator(mode="after")
    def _exactly_one_modifier_value(self) -> "BidModifierAdjustment":
        if self.adjustment_percent is None and self.bid_modifier is None:
            raise ValueError("adjustment_percent or bid_modifier is required")
        if self.adjustment_percent is not None and self.bid_modifier is not None:
            raise ValueError("Use either adjustment_percent or bid_modifier, not both")
        return self

    @property
    def direct_bid_modifier(self) -> int:
        if self.bid_modifier is not None:
            return self.bid_modifier
        assert self.adjustment_percent is not None
        return 100 + self.adjustment_percent

    @property
    def preview_adjustment_percent(self) -> int:
        if self.adjustment_percent is not None:
            return self.adjustment_percent
        assert self.bid_modifier is not None
        return self.bid_modifier - 100

    def to_preview_item(self, campaign_id: int | str) -> dict:
        item: dict = {
            "CampaignId": int(campaign_id),
            "AdjustmentPercent": self.preview_adjustment_percent,
            "BidModifier": self.direct_bid_modifier,
        }
        if self.modifier_id is not None:
            item["Id"] = self.modifier_id
        if self.type_hint:
            item["TypeHint"] = self.type_hint
        if self.age_range is not None:
            item["AgeRange"] = self.age_range
        if self.conditions:
            item["Conditions"] = self.conditions
        return item

    def to_direct_set_item(self) -> dict:
        if self.modifier_id is None:
            raise ValueError("modifier_id is required to build bidmodifiers.set payload")
        return {"Id": self.modifier_id, "BidModifier": self.direct_bid_modifier}


class BidModifierAgeAdjustment(BidModifierAdjustment):
    """Backward-compatible alias/model for the previous AGE_0_17 request shape."""

    age_range: Literal["AGE_0_17"] | None = Field(
        default="AGE_0_17",
        description="Under-18 age segment used for demographic exclusion previews.",
    )


BidModifierCreateType = Literal[
    "MOBILE_ADJUSTMENT",
    "TABLET_ADJUSTMENT",
    "DESKTOP_ADJUSTMENT",
    "DESKTOP_ONLY_ADJUSTMENT",
    "SMART_TV_ADJUSTMENT",
    "SMARTTV_ADJUSTMENT",
    "DEMOGRAPHICS_ADJUSTMENT",
    "RETARGETING_ADJUSTMENT",
    "REGIONAL_ADJUSTMENT",
    "VIDEO_ADJUSTMENT",
    "VIDEO_EXTENSION_ADJUSTMENT",
    "SMART_AD_ADJUSTMENT",
    "SERP_LAYOUT_ADJUSTMENT",
    "INCOME_GRADE_ADJUSTMENT",
    "AD_GROUP_ADJUSTMENT",
]


_CREATE_FAMILY_SETTINGS: dict[BidModifierCreateType, dict[str, Any]] = {
    "MOBILE_ADJUSTMENT": {
        "block": "MobileAdjustment",
        "required": set[str](),
        "optional": {"operating_system_type"},
    },
    "TABLET_ADJUSTMENT": {
        "block": "TabletAdjustment",
        "required": set[str](),
        "optional": {"operating_system_type"},
    },
    "DESKTOP_ADJUSTMENT": {
        "block": "DesktopAdjustment",
        "required": set[str](),
        "optional": set[str](),
    },
    "DESKTOP_ONLY_ADJUSTMENT": {
        "block": "DesktopOnlyAdjustment",
        "required": set[str](),
        "optional": set[str](),
    },
    "SMART_TV_ADJUSTMENT": {
        "block": "SmartTvAdjustment",
        "required": set[str](),
        "optional": set[str](),
    },
    "SMARTTV_ADJUSTMENT": {
        "block": "SmartTvAdjustment",
        "required": set[str](),
        "optional": set[str](),
    },
    "DEMOGRAPHICS_ADJUSTMENT": {
        "block": "DemographicsAdjustments",
        "array_block": True,
        "required": set[str](),
        "optional": {"gender", "age"},
    },
    "RETARGETING_ADJUSTMENT": {
        "block": "RetargetingAdjustments",
        "array_block": True,
        "required": {"retargeting_condition_id"},
        "optional": set[str](),
    },
    "REGIONAL_ADJUSTMENT": {
        "block": "RegionalAdjustments",
        "array_block": True,
        "required": {"region_id"},
        "optional": set[str](),
    },
    "VIDEO_ADJUSTMENT": {
        "block": "VideoAdjustment",
        "required": set[str](),
        "optional": set[str](),
    },
    "VIDEO_EXTENSION_ADJUSTMENT": {
        "block": "VideoAdjustment",
        "required": set[str](),
        "optional": set[str](),
    },
    "SMART_AD_ADJUSTMENT": {
        "block": "SmartAdAdjustment",
        "required": set[str](),
        "optional": set[str](),
    },
    "SERP_LAYOUT_ADJUSTMENT": {
        "block": "SerpLayoutAdjustments",
        "array_block": True,
        "required": {"serp_layout"},
        "optional": set[str](),
    },
    "INCOME_GRADE_ADJUSTMENT": {
        "block": "IncomeGradeAdjustments",
        "array_block": True,
        "required": {"grade"},
        "optional": set[str](),
    },
    "AD_GROUP_ADJUSTMENT": {
        "block": "AdGroupAdjustment",
        "required": set[str](),
        "optional": set[str](),
    },
}

_CREATE_ENUM_ALLOWED_VALUES: dict[str, set[str]] = {
    "operating_system_type": {"IOS", "ANDROID"},
    "gender": {"GENDER_MALE", "GENDER_FEMALE"},
    "age": {
        "AGE_0_17",
        "AGE_18_24",
        "AGE_25_34",
        "AGE_35_44",
        "AGE_45",
        "AGE_45_54",
        "AGE_55",
    },
    "serp_layout": {"ALONE", "SUGGEST"},
    "grade": {"VERY_HIGH", "HIGH", "ABOVE_AVERAGE"},
}


_FAMILY_BLOCK_FIELD_MAP: dict[str, str] = {
    "operating_system_type": "OperatingSystemType",
    "gender": "Gender",
    "age": "Age",
    "retargeting_condition_id": "RetargetingConditionId",
    "region_id": "RegionId",
    "serp_layout": "SerpLayout",
    "grade": "Grade",
    "enabled": "Enabled",
    "weather_type": "WeatherType",
    "temperature": "Temperature",
    "cloudiness": "Cloudiness",
    "precipitation": "Precipitation",
    "humidity": "Humidity",
    "wind": "Wind",
    "bid_modifier": "BidModifier",
}


class BidModifierCreateItem(BaseModel):
    """Single bid-modifier create item for ``bidmodifiers.add`` payload building."""

    @model_validator(mode="before")
    @classmethod
    def _reject_unsupported_create_types(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("type") == "WEATHER_ADJUSTMENT":
            raise ValueError(
                "WEATHER_ADJUSTMENT create is not supported by Yandex Direct "
                "bidmodifiers.add: live provider returned error_code=8000 "
                "unknown parameter WeatherAdjustment. Read/update existing "
                "weather modifiers by modifier_id only."
            )
        return data

    campaign_id: int | None = Field(default=None, ge=1)
    ad_group_id: int | None = Field(default=None, ge=1)
    type: BidModifierCreateType
    adjustment_percent: int | None = Field(
        default=None,
        ge=-100,
        le=1200,
        description=(
            "Human-facing adjustment percent. Converted to Direct coefficient with "
            "BidModifier = 100 + adjustment_percent."
        ),
    )
    bid_modifier: int | None = Field(
        default=None,
        ge=0,
        le=1300,
        description="Direct v5 BidModifier coefficient. Alternative to adjustment_percent.",
    )
    operating_system_type: str | None = None
    gender: str | None = None
    age: str | None = None
    retargeting_condition_id: int | None = Field(default=None, ge=1)
    region_id: int | None = Field(default=None, ge=1)
    serp_layout: str | None = None
    grade: str | None = None
    enabled: bool | None = None
    weather_type: str | None = None
    temperature: dict[str, Any] | None = None
    cloudiness: dict[str, Any] | None = None
    precipitation: dict[str, Any] | None = None
    humidity: dict[str, Any] | None = None
    wind: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_scope(self) -> "BidModifierCreateItem":
        if self.campaign_id is None and self.ad_group_id is None:
            raise ValueError("One of campaign_id or ad_group_id is required")
        if self.campaign_id is not None and self.ad_group_id is not None:
            raise ValueError("Only one of campaign_id or ad_group_id is allowed")
        if self.type == "AD_GROUP_ADJUSTMENT" and self.ad_group_id is None:
            raise ValueError("AD_GROUP_ADJUSTMENT requires ad_group_id scope")
        return self

    @model_validator(mode="after")
    def _validate_modifier_value(self) -> "BidModifierCreateItem":
        if self.adjustment_percent is None and self.bid_modifier is None:
            raise ValueError("adjustment_percent or bid_modifier is required")
        if self.adjustment_percent is not None and self.bid_modifier is not None:
            raise ValueError("Use either adjustment_percent or bid_modifier, not both")
        return self

    @model_validator(mode="after")
    def _validate_family_fields(self) -> "BidModifierCreateItem":
        settings = _CREATE_FAMILY_SETTINGS[self.type]
        required_fields = settings["required"]
        optional_fields = settings["optional"]
        allowed_fields = required_fields.union(optional_fields)

        present_fields = {
            name
            for name in (
                "age",
                "gender",
                "operating_system_type",
                "retargeting_condition_id",
                "region_id",
                "serp_layout",
                "grade",
                "enabled",
                "weather_type",
                "temperature",
                "cloudiness",
                "precipitation",
                "humidity",
                "wind",
            )
            if getattr(self, name) is not None
        }

        missing_fields = sorted(required_fields - present_fields)
        if missing_fields:
            raise ValueError(f"{self.type} requires: {', '.join(missing_fields)}")

        extra_fields = sorted(present_fields - allowed_fields)
        if extra_fields:
            raise ValueError(
                f"{self.type} does not allow optional fields: {', '.join(extra_fields)}"
            )

        for field_name, allowed_values in _CREATE_ENUM_ALLOWED_VALUES.items():
            value = getattr(self, field_name, None)
            if value is not None and value not in allowed_values:
                allowed_display = ", ".join(sorted(allowed_values))
                raise ValueError(
                    f"{field_name} must be one of: {allowed_display}"
                )

        return self

    @property
    def direct_bid_modifier(self) -> int:
        if self.bid_modifier is not None:
            return self.bid_modifier
        assert self.adjustment_percent is not None
        return 100 + self.adjustment_percent

    def to_direct_add_item(self) -> dict:
        item: dict = {}
        if self.campaign_id is not None:
            item["CampaignId"] = self.campaign_id
        else:
            item["AdGroupId"] = self.ad_group_id

        settings = _CREATE_FAMILY_SETTINGS[self.type]
        block: dict = {_FAMILY_BLOCK_FIELD_MAP["bid_modifier"]: self.direct_bid_modifier}

        allowed_fields = settings["required"].union(settings["optional"])
        for field_name in allowed_fields:
            value = getattr(self, field_name)
            if value is not None:
                block[_FAMILY_BLOCK_FIELD_MAP[field_name]] = value

        if settings.get("array_block"):
            item[settings["block"]] = [block]
        else:
            item[settings["block"]] = block
        return item


class BidModifiersCreateRequest(BaseModel):
    """Body model for a live-safe ``bidmodifiers.add`` create endpoint.

    ``dry_run=True`` is preview-only. A real ``bidmodifiers.add`` apply must be
    gated at the route/store layer by ``DIRECTPILOT_MODE=live_write``,
    ``approved=True``, valid ``idempotency_key``, and ``dry_run=False``.
    """

    dry_run: bool = True
    approved: bool = False
    idempotency_key: str | None = Field(default=None, min_length=6)
    items: list[BidModifierCreateItem] = Field(..., min_length=1, max_length=1000)
    reason: str | None = None

    def build_direct_add_payload(self) -> dict:
        return {"BidModifiers": [item.to_direct_add_item() for item in self.items]}


class BidModifierAddItemResult(BaseModel):
    """Per-item outcome from Direct v5 ``bidmodifiers.add`` ``AddResults``."""

    ids: list[int] = Field(default_factory=list)
    has_errors: bool = False
    has_warnings: bool = False
    errors: list["ProviderWarning"] = Field(default_factory=list)
    warnings: list["ProviderWarning"] = Field(default_factory=list)


class BidModifiersCreateResult(BaseModel):
    """Response for ``POST /yandex/campaigns/{campaign_id}/bid-modifiers/create``."""

    campaign_id: str
    mode: str
    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "yandex"
    read_only: bool = False
    audit_id: str
    payload_preview: dict | None = None
    readback: list[dict] | None = None
    provider_warnings: list["ProviderWarning"] = Field(default_factory=list)
    add_results: list["BidModifierAddItemResult"] | None = Field(
        default=None,
        description="Per-item outcomes from the v5 ``AddResults`` envelope.",
    )
    partial_failure: bool = Field(
        default=False,
        description=(
            "True when the top-level v5 add call succeeded but at least one "
            "``AddResults`` item contains provider errors or lacks returned ids."
        ),
    )
    yandex_units: int | None = None
    yandex_error: str | None = None
    message: str | None = None


class BidModifiersUpdateRequest(BaseModel):
    """Body model for a live-safe bid-modifier update endpoint.

    ``dry_run=True`` is preview-only. A real ``bidmodifiers.set`` apply must be
    gated at the store/route layer by ``DIRECTPILOT_MODE=live_write``,
    ``approved=True``, valid ``idempotency_key``, and ``dry_run=False``.
    """

    dry_run: bool = True
    approved: bool = False
    idempotency_key: str | None = Field(default=None, min_length=6)
    adjustments: list[BidModifierAdjustment] = Field(..., min_length=1, max_length=1000)
    reason: str | None = None

    def build_payload_preview(self, campaign_id: int | str) -> dict:
        return {
            "BidModifiers": [
                adjustment.to_preview_item(campaign_id) for adjustment in self.adjustments
            ]
        }

    def build_direct_set_payload(self) -> dict:
        return {
            "BidModifiers": [
                adjustment.to_direct_set_item() for adjustment in self.adjustments
            ]
        }


class BidModifierSetItemResult(BaseModel):
    """Per-item outcome from Direct v5 ``bidmodifiers.set`` ``SetResults``.

    Only redacted provider fields are surfaced. ``has_errors=True`` means
    this item was not safely accepted as applied; the aggregate response must
    use ``applied=False`` and ``partial_failure=True``.
    """

    modifier_id: int
    has_errors: bool = False
    has_warnings: bool = False
    errors: list["ProviderWarning"] = Field(default_factory=list)
    warnings: list["ProviderWarning"] = Field(default_factory=list)


class BidModifiersUpdateResult(BaseModel):
    """Response for ``POST /yandex/campaigns/{campaign_id}/bid-modifiers``."""

    campaign_id: str
    mode: str
    dry_run: bool
    applied: bool
    source: Literal["mock", "yandex"] = "yandex"
    read_only: bool = False
    audit_id: str
    payload_preview: dict | None = None
    readback: list[dict] | None = None
    provider_warnings: list["ProviderWarning"] = Field(default_factory=list)
    set_results: list["BidModifierSetItemResult"] | None = Field(
        default=None,
        description=(
            "Per-item outcomes from the v5 ``SetResults`` envelope. "
            "Populated on live apply when Direct returns item-level results."
        ),
    )
    partial_failure: bool = Field(
        default=False,
        description=(
            "True when the top-level v5 call succeeded but at least one "
            "``SetResults`` item contains provider errors. In this case "
            "``applied`` is false."
        ),
    )
    yandex_units: int | None = None
    yandex_error: str | None = None
