from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


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
