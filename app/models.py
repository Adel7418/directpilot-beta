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
