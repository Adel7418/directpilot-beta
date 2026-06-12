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


class UtmGenerateRequest(BaseModel):
    landing_url: HttpUrl
    campaign: str = Field(..., min_length=1)
    content: str = Field(default="{ad_id}", min_length=1)
    term: str = Field(default="{keyword}", min_length=1)


class UtmGenerateResult(BaseModel):
    url: str
    requires_approval: bool = False


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
    impressions: int
    clicks: int
    ctr: float


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
