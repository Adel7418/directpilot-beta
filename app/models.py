from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


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


class CampaignDraftRequest(BaseModel):
    business_type: str = Field(..., min_length=2)
    region: str = Field(..., min_length=2)
    monthly_budget: float = Field(..., gt=0)
    landing_url: str


class CampaignDraft(BaseModel):
    id: str
    status: Literal["draft"] = "draft"
    groups: list[str]
    keywords: list[str]
    risk_level: Literal["low", "medium", "high"] = "medium"
    requires_approval: bool = True


class CampaignDraftKeywordsUpdate(BaseModel):
    keywords: list[str] = Field(..., min_length=1)


class CampaignDraftList(BaseModel):
    items: list[CampaignDraft]


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


class AuditEvent(BaseModel):
    id: str
    actor: str
    action: str
    entity: str
    dry_run: bool = True


class AuditLog(BaseModel):
    items: list[AuditEvent]


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
