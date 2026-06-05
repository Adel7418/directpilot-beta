from fastapi import FastAPI, HTTPException

from app.config import get_settings
from app.models import (
    ApplyActionRequest,
    ApplyActionResult,
    ApprovalResult,
    AuditEvent,
    AuditLog,
    Campaign,
    CampaignDraft,
    CampaignDraftRequest,
    CampaignList,
    Recommendation,
    RecommendationList,
    ReportSummary,
)

app = FastAPI(
    title="DirectPilot Beta API",
    version="0.1.0",
    description="Standalone API-first beta app for safe Yandex Direct automation.",
)


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "service": "directpilot-beta",
        "mode": settings.directpilot_mode,
        "yandex": settings.safe_status(),
    }


@app.get("/campaigns", response_model=CampaignList)
def list_campaigns() -> CampaignList:
    return CampaignList(
        items=[
            Campaign(
                id="cmp_mock_local_services",
                name="Mock: локальные услуги",
                business_type="local_services",
                status="draft_readonly",
                spend=1250.0,
                clicks=42,
            )
        ]
    )


@app.get("/reports/summary", response_model=ReportSummary)
def report_summary() -> ReportSummary:
    return ReportSummary(
        spend=1250.0,
        clicks=42,
        impressions=2100,
        ctr=2.0,
        cpc=29.76,
        conversions=None,
        cpa=None,
    )


@app.post("/campaign-drafts", response_model=CampaignDraft)
def create_campaign_draft(payload: CampaignDraftRequest) -> CampaignDraft:
    return CampaignDraft(
        id="draft_mock_001",
        groups=[f"{payload.business_type}: базовая группа"],
        keywords=[f"{payload.business_type} {payload.region}", f"заказать {payload.business_type}"],
    )


@app.get("/recommendations", response_model=RecommendationList)
def list_recommendations() -> RecommendationList:
    return RecommendationList(
        items=[
            Recommendation(
                id="rec_mock_pause_keyword",
                action_id="act_mock_pause_keyword",
                reason="Ключ потратил бюджет в mock-отчёте и не имеет конверсий.",
                risk_level="low",
            )
        ]
    )


@app.post("/recommendations/{recommendation_id}/approve", response_model=ApprovalResult)
def approve_recommendation(recommendation_id: str) -> ApprovalResult:
    return ApprovalResult(recommendation_id=recommendation_id, status="approved")


@app.post("/recommendations/{recommendation_id}/reject", response_model=ApprovalResult)
def reject_recommendation(recommendation_id: str) -> ApprovalResult:
    return ApprovalResult(recommendation_id=recommendation_id, status="rejected")


@app.post("/actions/{action_id}/apply", response_model=ApplyActionResult)
def apply_action(action_id: str, payload: ApplyActionRequest) -> ApplyActionResult:
    if not payload.approved:
        raise HTTPException(status_code=409, detail="Action requires explicit approval before apply")
    return ApplyActionResult(
        action_id=action_id,
        dry_run=payload.dry_run,
        applied=not payload.dry_run,
        risk_level="low",
        audit_id="audit_mock_001",
    )


@app.get("/audit-log", response_model=AuditLog)
def audit_log() -> AuditLog:
    return AuditLog(
        items=[
            AuditEvent(
                id="audit_mock_001",
                actor="system",
                action="mock_healthcheck",
                entity="directpilot-beta",
            )
        ]
    )
