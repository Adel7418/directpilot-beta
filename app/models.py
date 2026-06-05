from typing import Literal

from pydantic import BaseModel, Field


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


class Recommendation(BaseModel):
    id: str
    action_id: str
    reason: str
    risk_level: Literal["low", "medium", "high"]
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
