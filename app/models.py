from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


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


class BudgetUpdate(BaseModel):
    daily_budget: float | None = Field(default=None, ge=0)
    monthly_budget: float | None = Field(default=None, ge=0)
    strategy: Literal["manual", "max_clicks", "max_conversions", "weekly_budget"] | None = None


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
