from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.services import check_yandex_direct
from app.models import (
    WEEK_DAY_NAMES,
    AdCreate,
    AdGroupCreate,
    AdGroupUpdate,
    AdUpdate,
    ApiErrorResponse,
    ApplyActionRequest,
    ApplyActionResult,
    ApprovalResult,
    AuditCheck,
    AuditLog,
    BidUpdate,
    BudgetSimulationRequest,
    BudgetSimulationResult,
    BudgetUpdate,
    CampaignAuditResult,
    CampaignDraft,
    CampaignDraftBaseUpdate,
    CampaignDraftKeywordsAdd,
    CampaignDraftKeywordsRemove,
    CampaignDraftKeywordsUpdate,
    CampaignDraftList,
    CampaignDraftRequest,
    CampaignList,
    GenerateStructureRequest,
    GenerateStructureResult,
    LiveCreateCampaignRequest,
    LiveCreateCampaignResult,
    NegativeKeywordsReplace,
    PreviewPayload,
    RecommendationList,
    ReportSummary,
    SemanticChangeApplyRequest,
    SemanticChangeApplyResult,
    SemanticChangePackage,
    SemanticChangeRequest,
    UtmGenerateRequest,
    UtmGenerateResult,
    ValidationResult,
    YandexAccountBalance,
    YandexAccountBalanceResult,
    YandexAdsBusinessAttachRequest,
    YandexAdsBusinessAttachResult,
    YandexAd,
    YandexAdGroup,
    YandexAdGroupList,
    YandexAdList,
    YandexAdAssetItem,
    YandexAdAssetsMissing,
    YandexAdAssetsResult,
    YandexBusinessAssetItem,
    YandexCampaign,
    YandexCampaignFinance,
    YandexCampaignFinanceList,
    YandexCampaignList,
    YandexControlRequest,
    YandexControlResult,
    YandexKeyword,
    YandexKeywordList,
    YandexMetrikaResult,
    YandexRawResult,
    YandexSearchApiResult,
    YandexSearchQueriesReport,
    YandexSearchQuery,
    YandexSitelinkItem,
    YandexSitelinkSetItem,
    YandexTimeTargetingRequest,
    YandexTimeTargetingHourly,
    YandexTimeTargetingReadResult,
    YandexTimeTargetingResult,
    YandexTimeTargetingSchedule,
    YandexVCardRequest,
    YandexVCardResult,
    YandexVCardAssetItem,
    LiveAdCreateRequest,
    LiveAdCreateResult,
    AdsModerateRequest,
    AdsModerateResult,
)
from app.store import store
from app.yandex_direct import YandexDirectClient, YandexDirectError
from app.yandex_facade import mock_yandex
from app.yandex_metrika import (
    YandexMetrikaClient,
    YandexMetrikaError,
    YandexMetrikaMissingTokenError,
)
from app.yandex_search_wordstat import (
    YandexSearchWordstatClient,
    YandexSearchWordstatError,
    YandexSearchWordstatMissingKeyError,
)


def get_yandex_client(
    settings: Settings = Depends(get_settings),
) -> YandexDirectClient | None:
    """Build a YandexDirectClient for credentialed Yandex modes.

    Mock mode stays no-network. sandbox/live_readonly/live_write can all read
    real Direct data; write permission is enforced later by store.yandex_control.
    Missing-token errors are raised inside YandexDirectClient._call(), so dry_run
    paths remain safe while credentialed calls return redacted 502 errors.
    """
    if settings.directpilot_mode not in ("sandbox", "live_readonly", "live_write"):
        return None
    return YandexDirectClient(settings=settings)

app = FastAPI(
    title="DirectPilot Beta API",
    version="0.2.1",
    description="Standalone API-first beta app for safe Yandex Direct automation.",
)


YANDEX_DIRECT_ERROR_RESPONSES = {
    502: {"model": ApiErrorResponse, "description": "Yandex Direct upstream error"},
    503: {"model": ApiErrorResponse, "description": "YANDEX_OAUTH_TOKEN is not configured"},
}


WORDSTAT_ERROR_RESPONSES = {
    502: {"model": ApiErrorResponse, "description": "Yandex Search API upstream error"},
    503: {"model": ApiErrorResponse, "description": "YANDEX_SEARCH_API_KEY is not configured"},
}


METRIKA_ERROR_RESPONSES = {
    502: {"model": ApiErrorResponse, "description": "Yandex Metrika upstream error"},
    503: {"model": ApiErrorResponse, "description": "YANDEX_METRIKA_OAUTH_TOKEN is not configured"},
}


# ---------------------------------------------------------------------------
# Non-product guard
# ---------------------------------------------------------------------------
# Demo/UI routes (HTML home + 6 /demo/* pages) are not part of the DirectPilot
# product surface: they were built for early stakeholder reviews and are no
# longer shipped. They are registered only as explicit non-product guards so
# old links/bookmarks get a 404 instead of silently routing elsewhere. These
# guard handlers are NOT included in the OpenAPI schema and must not return
# product/demo data.

_NON_PRODUCT_PATHS = {
    "/",
    "/demo/yandex-status",
    "/demo/campaigns",
    "/demo/report",
    "/demo/recommendations",
    "/demo/tools",
    "/demo/security-approval",
}


def _non_product_guard(path: str):
    """Return a 404 JSONResponse for retired demo/UI paths, or None.

    Keeping this as a small explicit allow-list (rather than re-registering
    the original HTML routes) ensures the demo surface cannot accidentally
    come back online and cannot leak into OpenAPI.
    """
    if path in _NON_PRODUCT_PATHS:
        return JSONResponse(
            status_code=404,
            content={
                "detail": "Not part of DirectPilot product surface",
                "path": path,
            },
        )
    return None


@app.get("/", include_in_schema=False)
def _non_product_root():
    return _non_product_guard("/")


@app.get("/demo/yandex-status", include_in_schema=False)
def _non_product_yandex_status():
    return _non_product_guard("/demo/yandex-status")


@app.get("/demo/campaigns", include_in_schema=False)
def _non_product_demo_campaigns():
    return _non_product_guard("/demo/campaigns")


@app.get("/demo/report", include_in_schema=False)
def _non_product_demo_report():
    return _non_product_guard("/demo/report")


@app.get("/demo/recommendations", include_in_schema=False)
def _non_product_demo_recommendations():
    return _non_product_guard("/demo/recommendations")


@app.get("/demo/tools", include_in_schema=False)
def _non_product_demo_tools():
    return _non_product_guard("/demo/tools")


@app.get("/demo/security-approval", include_in_schema=False)
def _non_product_demo_security_approval():
    return _non_product_guard("/demo/security-approval")


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "service": "directpilot-beta",
        "mode": settings.directpilot_mode,
        "yandex": settings.safe_status(),
    }


@app.get("/integrations/yandex/direct/status")
def yandex_direct_status() -> dict:
    settings = get_settings()
    return check_yandex_direct(settings)


@app.post("/utm/generate", response_model=UtmGenerateResult)
def generate_utm(payload: UtmGenerateRequest) -> UtmGenerateResult:
    parts = urlsplit(str(payload.landing_url))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(
        {
            "utm_source": "yandex",
            "utm_medium": "cpc",
            "utm_campaign": payload.campaign,
            "utm_content": payload.content,
            "utm_term": payload.term,
        }
    )
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return UtmGenerateResult(url=url)


@app.post("/simulations/budget", response_model=BudgetSimulationResult)
def simulate_budget(payload: BudgetSimulationRequest) -> BudgetSimulationResult:
    estimated_clicks = int(payload.daily_budget // payload.avg_cpc)
    estimated_conversions = round(estimated_clicks * payload.conversion_rate / 100, 2)
    return BudgetSimulationResult(
        estimated_clicks=estimated_clicks,
        estimated_conversions=estimated_conversions,
        estimated_spend=round(estimated_clicks * payload.avg_cpc, 2),
    )


@app.get("/audit/campaigns", response_model=CampaignAuditResult)
def audit_campaigns() -> CampaignAuditResult:
    return CampaignAuditResult(
        items=[
            AuditCheck(
                code="missing_utm",
                severity="medium",
                title="Нет UTM-разметки",
                recommendation="Добавить UTM, чтобы связать клики с отчётами и заявками.",
            ),
            AuditCheck(
                code="no_metrica_goal",
                severity="high",
                title="Не выбрана цель Метрики",
                recommendation="Связать основную цель Метрики с кампанией до включения auto-apply.",
            ),
            AuditCheck(
                code="high_cpc",
                severity="medium",
                title="Высокая цена клика",
                recommendation="Запустить dry-run симуляцию бюджета и проверить ставки по ключам.",
            ),
        ]
    )


@app.get("/campaigns", response_model=CampaignList)
def list_campaigns() -> CampaignList:
    return CampaignList(items=list(store.campaigns.values()))


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


# ---------------------------------------------------------------------------
# Campaign draft constructor
# ---------------------------------------------------------------------------


@app.post("/campaign-drafts", response_model=CampaignDraft)
def create_campaign_draft(payload: CampaignDraftRequest) -> CampaignDraft:
    return store.create_draft(payload)


@app.get("/campaign-drafts", response_model=CampaignDraftList)
def list_campaign_drafts() -> CampaignDraftList:
    return CampaignDraftList(items=list(store.drafts.values()))


@app.get("/campaign-drafts/{draft_id}", response_model=CampaignDraft)
def get_campaign_draft(draft_id: str) -> CampaignDraft:
    try:
        return store.drafts[draft_id]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Campaign draft not found") from exc


@app.patch("/campaign-drafts/{draft_id}", response_model=CampaignDraft)
def patch_campaign_draft(draft_id: str, payload: CampaignDraftBaseUpdate) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    return store.update_draft_base(draft_id, payload.model_dump(exclude_unset=True))


@app.patch("/campaign-drafts/{draft_id}/keywords", response_model=CampaignDraft)
def update_campaign_draft_keywords(
    draft_id: str, payload: CampaignDraftKeywordsUpdate
) -> CampaignDraft:
    try:
        return store.replace_keywords(draft_id, payload.keywords)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Campaign draft not found") from exc


@app.post("/campaign-drafts/{draft_id}/keywords", response_model=CampaignDraft)
def add_campaign_draft_keywords(
    draft_id: str, payload: CampaignDraftKeywordsAdd
) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    return store.append_keywords(draft_id, payload.keywords)


@app.delete("/campaign-drafts/{draft_id}/keywords", response_model=CampaignDraft)
def delete_campaign_draft_keywords(
    draft_id: str, payload: CampaignDraftKeywordsRemove
) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    return store.remove_keywords(draft_id, payload.keywords)


@app.patch(
    "/campaign-drafts/{draft_id}/negative-keywords", response_model=CampaignDraft
)
def patch_negative_keywords(
    draft_id: str, payload: NegativeKeywordsReplace
) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    return store.replace_negative_keywords(draft_id, payload)


@app.post("/campaign-drafts/{draft_id}/ad-groups", response_model=CampaignDraft)
def create_ad_group(draft_id: str, payload: AdGroupCreate) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    return store.create_ad_group(draft_id, payload)


@app.patch(
    "/campaign-drafts/{draft_id}/ad-groups/{group_id}", response_model=CampaignDraft
)
def update_ad_group(
    draft_id: str, group_id: str, payload: AdGroupUpdate
) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    try:
        return store.update_ad_group(draft_id, group_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Ad group not found") from exc


@app.delete(
    "/campaign-drafts/{draft_id}/ad-groups/{group_id}", response_model=CampaignDraft
)
def delete_ad_group(draft_id: str, group_id: str) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    try:
        return store.delete_ad_group(draft_id, group_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Ad group not found") from exc


@app.post("/campaign-drafts/{draft_id}/ads", response_model=CampaignDraft)
def create_ad(draft_id: str, payload: AdCreate) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    try:
        return store.create_ad(draft_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Ad group not found") from exc


@app.patch("/campaign-drafts/{draft_id}/ads/{ad_id}", response_model=CampaignDraft)
def update_ad(draft_id: str, ad_id: str, payload: AdUpdate) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    try:
        return store.update_ad(draft_id, ad_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Ad not found") from exc


@app.delete("/campaign-drafts/{draft_id}/ads/{ad_id}", response_model=CampaignDraft)
def delete_ad(draft_id: str, ad_id: str) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    try:
        return store.delete_ad(draft_id, ad_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Ad not found") from exc


@app.post(
    "/campaign-drafts/{draft_id}/generate-structure",
    response_model=GenerateStructureResult,
)
def generate_structure(
    draft_id: str, payload: GenerateStructureRequest
) -> GenerateStructureResult:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    draft = store.generate_structure(draft_id, payload)
    return GenerateStructureResult(
        ad_groups=draft.ad_groups,
        keywords=draft.keywords,
        negative_keywords=draft.negative_keywords,
        ads=draft.ads,
    )


@app.post("/campaign-drafts/{draft_id}/validate", response_model=ValidationResult)
def validate_draft(draft_id: str) -> ValidationResult:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    return store.validate_draft(draft_id)


@app.get("/campaign-drafts/{draft_id}/preview", response_model=PreviewPayload)
def preview_draft(draft_id: str) -> PreviewPayload:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    return store.preview_draft(draft_id)


@app.patch("/campaign-drafts/{draft_id}/budget", response_model=CampaignDraft)
def patch_draft_budget(draft_id: str, payload: BudgetUpdate) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    return store.update_budget(draft_id, payload)


@app.patch("/campaign-drafts/{draft_id}/bids", response_model=CampaignDraft)
def patch_draft_bids(draft_id: str, payload: BidUpdate) -> CampaignDraft:
    if draft_id not in store.drafts:
        raise HTTPException(status_code=404, detail="Campaign draft not found")
    return store.update_bids(draft_id, payload)


# ---------------------------------------------------------------------------
# Recommendations / approval / apply
# ---------------------------------------------------------------------------


@app.get("/recommendations", response_model=RecommendationList)
def list_recommendations() -> RecommendationList:
    return RecommendationList(items=list(store.recommendations.values()))


@app.post("/recommendations/{recommendation_id}/approve", response_model=ApprovalResult)
def approve_recommendation(recommendation_id: str) -> ApprovalResult:
    if recommendation_id not in store.recommendations:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    store.recommendations[recommendation_id].status = "approved"
    store.append_audit("recommendation_approved", recommendation_id)
    return ApprovalResult(recommendation_id=recommendation_id, status="approved")


@app.post("/recommendations/{recommendation_id}/reject", response_model=ApprovalResult)
def reject_recommendation(recommendation_id: str) -> ApprovalResult:
    if recommendation_id not in store.recommendations:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    store.recommendations[recommendation_id].status = "rejected"
    store.append_audit("recommendation_rejected", recommendation_id)
    return ApprovalResult(recommendation_id=recommendation_id, status="rejected")


@app.post("/actions/{action_id}/apply", response_model=ApplyActionResult)
def apply_action(action_id: str, payload: ApplyActionRequest) -> ApplyActionResult:
    if not payload.approved:
        raise HTTPException(status_code=409, detail="Action requires explicit approval before apply")
    if payload.idempotency_key in store.apply_results_by_key:
        return store.apply_results_by_key[payload.idempotency_key]
    recommendation = next((r for r in store.recommendations.values() if r.action_id == action_id), None)
    if recommendation is None:
        raise HTTPException(status_code=404, detail="Action not found")
    if recommendation.status != "approved":
        raise HTTPException(status_code=409, detail="Recommendation must be approved before apply")
    event = store.append_audit("action_applied", action_id, dry_run=payload.dry_run)
    recommendation.status = "applied"
    result = ApplyActionResult(
        action_id=action_id,
        dry_run=payload.dry_run,
        applied=not payload.dry_run,
        risk_level=recommendation.risk_level,
        audit_id=event.id,
    )
    store.apply_results_by_key[payload.idempotency_key] = result
    return result


@app.get("/audit-log", response_model=AuditLog)
def audit_log() -> AuditLog:
    return AuditLog(items=store.audit_events)


# ---------------------------------------------------------------------------
# Yandex Direct read-only facade
#
# Mock mode returns deterministic in-memory data (no network).
# Sandbox / live_readonly / live_write hit the real Direct API v5
# (campaigns.get / adgroups.get / ads.get / keywords.get). These are all
# read-only — the live modes never trigger a write call from this facade.
# ---------------------------------------------------------------------------


def _yandex_error_to_502(exc: YandexDirectError) -> HTTPException:
    """Translate a YandexDirectError into an HTTP 502 with no token in detail."""
    return HTTPException(
        status_code=502,
        detail={
            "error_type": "YandexDirectError",
            "message": str(exc),
        },
    )


# --- mapping helpers --------------------------------------------------------
#
# These helpers are intentionally permissive: Direct API v5 may omit
# fields (status on a fresh ad group, daily_budget on a campaign created
# without a budget cap, etc.). We never let a missing field crash the
# endpoint — we fall back to safe defaults.
# ---------------------------------------------------------------------------


def _extract_campaigns(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Map Direct API v5 campaigns.get result → list of YandexCampaign dicts."""
    items: list[dict[str, Any]] = []
    if not isinstance(result, dict):
        return items
    raw = result.get("Campaigns") or result.get("campaigns") or []
    for c in raw:
        if not isinstance(c, dict):
            continue
        daily_budget = c.get("DailyBudget")
        if isinstance(daily_budget, dict):
            # Yandex returns amount in micro-units (1/1_000_000 of currency).
            amount = daily_budget.get("Amount", 0) or 0
            try:
                budget_value = float(amount) / 1_000_000
            except (TypeError, ValueError):
                budget_value = 0.0
        else:
            budget_value = 0.0
        items.append(
            {
                "id": str(c.get("Id") or c.get("id") or ""),
                "name": str(c.get("Name") or c.get("name") or ""),
                "status": str(c.get("Status") or c.get("status") or "UNKNOWN"),
                "type": str(c.get("Type") or c.get("type") or "UNKNOWN"),
                "daily_budget": budget_value,
            }
        )
    return items


def _extract_ad_groups(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not isinstance(result, dict):
        return items
    raw = result.get("AdGroups") or result.get("adgroups") or []
    for g in raw:
        if not isinstance(g, dict):
            continue
        items.append(
            {
                "id": str(g.get("Id") or g.get("id") or ""),
                "campaign_id": str(g.get("CampaignId") or g.get("campaignId") or ""),
                "name": str(g.get("Name") or g.get("name") or ""),
                "status": str(g.get("Status") or g.get("status") or "UNKNOWN"),
            }
        )
    return items


def _extract_ads(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not isinstance(result, dict):
        return items
    raw = result.get("Ads") or result.get("ads") or []
    for a in raw:
        if not isinstance(a, dict):
            continue
        text_ad = a.get("TextAd") or a.get("textAd")
        title = ""
        if isinstance(text_ad, dict):
            raw_title = text_ad.get("Title") or text_ad.get("title")
            if isinstance(raw_title, str):
                title = raw_title
        items.append(
            {
                "id": str(a.get("Id") or a.get("id") or ""),
                "ad_group_id": str(a.get("AdGroupId") or a.get("adGroupId") or ""),
                "campaign_id": str(a.get("CampaignId") or a.get("campaignId") or ""),
                "title": title,
                "status": str(a.get("Status") or a.get("status") or "UNKNOWN"),
            }
        )
    return items


def _extract_keywords(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not isinstance(result, dict):
        return items
    raw = result.get("Keywords") or result.get("keywords") or []
    for k in raw:
        if not isinstance(k, dict):
            continue
        items.append(
            {
                "id": str(k.get("Id") or k.get("id") or ""),
                "ad_group_id": str(k.get("AdGroupId") or k.get("adGroupId") or ""),
                "phrase": str(k.get("Keyword") or k.get("keyword") or ""),
                "status": str(k.get("Status") or k.get("status") or "UNKNOWN"),
            }
        )
    return items


def _is_live_read_mode(settings: Settings) -> bool:
    """True for any non-mock mode that should use the real get endpoints."""
    return settings.directpilot_mode in ("sandbox", "live_readonly", "live_write")


# --- endpoint handlers ------------------------------------------------------


@app.get("/yandex/campaigns", response_model=YandexCampaignList)
def yandex_campaigns(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexCampaignList:
    if _is_live_read_mode(settings) and client is not None:
        try:
            response = client.campaigns_get()
        except YandexDirectError as exc:
            raise _yandex_error_to_502(exc) from exc
        if not response.get("ok"):
            err = response.get("error") or {}
            raise HTTPException(
                status_code=502,
                detail={
                    "error_type": "YandexDirectError",
                    "message": (
                        f"Yandex Direct rejected campaigns.get: "
                        f"error_code={err.get('error_code')!r}"
                    ),
                },
            )
        items = _extract_campaigns(response.get("result"))
        return YandexCampaignList(
            items=[YandexCampaign(**c) for c in items],
            source="yandex",
            read_only=True,
        )
    return YandexCampaignList(
        items=[YandexCampaign(**campaign) for campaign in mock_yandex.list_campaigns()],
        source="mock",
        read_only=True,
    )


@app.get(
    "/yandex/campaigns/{campaign_id}/ad-groups",
    response_model=YandexAdGroupList,
)
def yandex_ad_groups(
    campaign_id: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexAdGroupList:
    if _is_live_read_mode(settings) and client is not None:
        try:
            response = client.adgroups_get(campaign_id)
        except YandexDirectError as exc:
            raise _yandex_error_to_502(exc) from exc
        if not response.get("ok"):
            err = response.get("error") or {}
            raise HTTPException(
                status_code=502,
                detail={
                    "error_type": "YandexDirectError",
                    "message": (
                        f"Yandex Direct rejected adgroups.get: "
                        f"error_code={err.get('error_code')!r}"
                    ),
                },
            )
        items = _extract_ad_groups(response.get("result"))
        return YandexAdGroupList(
            items=[YandexAdGroup(**g) for g in items],
            source="yandex",
            read_only=True,
        )
    items = [YandexAdGroup(**g) for g in mock_yandex.list_ad_groups(campaign_id)]
    return YandexAdGroupList(items=items, source="mock", read_only=True)


@app.get("/yandex/campaigns/{campaign_id}/ads", response_model=YandexAdList)
def yandex_ads(
    campaign_id: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexAdList:
    if _is_live_read_mode(settings) and client is not None:
        try:
            response = client.ads_get(campaign_id)
        except YandexDirectError as exc:
            raise _yandex_error_to_502(exc) from exc
        if not response.get("ok"):
            err = response.get("error") or {}
            raise HTTPException(
                status_code=502,
                detail={
                    "error_type": "YandexDirectError",
                    "message": (
                        f"Yandex Direct rejected ads.get: "
                        f"error_code={err.get('error_code')!r}"
                    ),
                },
            )
        items = _extract_ads(response.get("result"))
        return YandexAdList(
            items=[YandexAd(**a) for a in items],
            source="yandex",
            read_only=True,
        )
    items = [YandexAd(**a) for a in mock_yandex.list_ads(campaign_id)]
    return YandexAdList(items=items, source="mock", read_only=True)


@app.get(
    "/yandex/campaigns/{campaign_id}/keywords",
    response_model=YandexKeywordList,
)
def yandex_keywords(
    campaign_id: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexKeywordList:
    if _is_live_read_mode(settings) and client is not None:
        try:
            response = client.keywords_get(campaign_id)
        except YandexDirectError as exc:
            raise _yandex_error_to_502(exc) from exc
        if not response.get("ok"):
            err = response.get("error") or {}
            raise HTTPException(
                status_code=502,
                detail={
                    "error_type": "YandexDirectError",
                    "message": (
                        f"Yandex Direct rejected keywords.get: "
                        f"error_code={err.get('error_code')!r}"
                    ),
                },
            )
        items = _extract_keywords(response.get("result"))
        return YandexKeywordList(
            items=[YandexKeyword(**kw) for kw in items],
            source="yandex",
            read_only=True,
        )
    items = mock_yandex.list_keywords(campaign_id)
    return YandexKeywordList(
        items=[YandexKeyword(**kw) for kw in items],
        source="mock",
        read_only=True,
    )


def _raw_yandex_result(service: str, method: str, response: dict[str, Any]) -> YandexRawResult:
    if not response.get("ok"):
        err = response.get("error") or {}
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": f"Yandex Direct rejected {service}.{method}: error_code={err.get('error_code')!r}",
            },
        )
    return YandexRawResult(
        service=service,
        method=method,
        data=response.get("result"),
        source="yandex",
        read_only=True,
    )


def _require_yandex_read_client(
    settings: Settings,
    client: YandexDirectClient | None,
) -> YandexDirectClient:
    if not _is_live_read_mode(settings) or client is None:
        raise HTTPException(
            status_code=409,
            detail="This endpoint requires sandbox, live_readonly, or live_write mode with Yandex credentials",
        )
    return client


def _call_raw_read(
    settings: Settings,
    client: YandexDirectClient | None,
    service: str,
    method: str,
    call,
) -> YandexRawResult:
    direct = _require_yandex_read_client(settings, client)
    try:
        response = call(direct)
    except YandexDirectError as exc:
        raise _yandex_error_to_502(exc) from exc
    return _raw_yandex_result(service, method, response)


@app.get("/yandex/campaigns/{campaign_id}/bids", response_model=YandexRawResult)
def yandex_bids(
    campaign_id: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "bids", "get", lambda c: c.bids_get(campaign_id))


@app.get("/yandex/changes/check", response_model=YandexRawResult)
def yandex_changes_check(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "changes", "check", lambda c: c.changes_check())


@app.get("/yandex/changes", response_model=YandexRawResult)
def yandex_changes_get(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "changes", "get", lambda c: c.changes_get())


@app.get("/yandex/dictionaries", response_model=YandexRawResult)
def yandex_dictionaries(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "dictionaries", "get", lambda c: c.dictionaries_get())


@app.get("/yandex/campaigns/{campaign_id}/bid-modifiers", response_model=YandexRawResult)
def yandex_bid_modifiers(
    campaign_id: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "bidmodifiers", "get", lambda c: c.bidmodifiers_get(campaign_id))


@app.get("/yandex/campaigns/{campaign_id}/negative-keywords", response_model=YandexRawResult)
def yandex_negative_keywords(
    campaign_id: str,
    ids: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(
        settings,
        client,
        "negativekeywordsharedsets",
        "get",
        lambda c: c.negativekeywords_get(campaign_id, _csv_ints(ids)),
    )


@app.get("/yandex/retargeting-lists", response_model=YandexRawResult)
def yandex_retargeting_lists(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "retargetinglists", "get", lambda c: c.retargetinglists_get())


@app.get("/yandex/campaigns/{campaign_id}/audience-targets", response_model=YandexRawResult)
def yandex_audience_targets(
    campaign_id: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "audiencetargets", "get", lambda c: c.audiencetargets_get(campaign_id))


@app.get("/yandex/sitelinks", response_model=YandexRawResult)
def yandex_sitelinks(
    ids: list[int] | None = Query(default=None),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "sitelinks", "get", lambda c: c.sitelinks_get(ids=ids, limit=limit, offset=offset))


@app.get(
    "/yandex/campaigns/{campaign_id}/ad-assets",
    response_model=YandexAdAssetsResult,
)
def yandex_campaign_ad_assets(
    campaign_id: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexAdAssetsResult:
    """Read-only campaign ad-assets aggregator for marketing/audit.

    Returns ads with extended TextAd fields, resolved sitelink sets,
    businesses, vcards, and callouts (not yet implemented).
    No writes — only get/read methods.
    """
    if _is_live_read_mode(settings):
        if client is None:
            raise HTTPException(
                status_code=409,
                detail="This endpoint requires sandbox, live_readonly, or live_write mode with Yandex credentials",
            )
        direct = client
        try:
            ads_response = direct.ads_get_detailed(campaign_id)
        except YandexDirectError as exc:
            raise _yandex_error_to_502(exc) from exc

        if not ads_response.get("ok"):
            err = ads_response.get("error") or {}
            raise HTTPException(
                status_code=502,
                detail={
                    "error_type": "YandexDirectError",
                    "message": (
                        f"Yandex Direct rejected ads.get: "
                        f"error_code={err.get('error_code')!r}"
                    ),
                },
            )

        raw_ads = (ads_response.get("result") or {}).get("Ads") or []

        # Extract ads with extended fields
        ads: list[YandexAdAssetItem] = []
        sitelink_set_ids: set[int] = set()
        business_ids: set[int] = set()
        vcard_ids: set[int] = set()

        for a in raw_ads:
            if not isinstance(a, dict):
                continue
            text_ad = a.get("TextAd") or {}
            sl_set_id = text_ad.get("SitelinkSetId")
            biz_id = text_ad.get("BusinessId")
            vc_id = text_ad.get("VCardId")

            ad_item = YandexAdAssetItem(
                id=str(a.get("Id") or ""),
                ad_group_id=str(a.get("AdGroupId") or ""),
                campaign_id=str(a.get("CampaignId") or campaign_id),
                status=str(a.get("Status") or "UNKNOWN"),
                state=str(a.get("State") or "UNKNOWN"),
                type=str(a.get("Type") or "TEXT_AD"),
                title=str(text_ad.get("Title") or ""),
                title2=text_ad.get("Title2") if isinstance(text_ad.get("Title2"), str) else None,
                text=str(text_ad.get("Text") or ""),
                href=str(text_ad.get("Href") or ""),
                display_url_path=text_ad.get("DisplayUrlPath") if isinstance(text_ad.get("DisplayUrlPath"), str) else None,
                sitelink_set_id=str(sl_set_id) if sl_set_id is not None else None,
                business_id=str(biz_id) if biz_id is not None else None,
                vcard_id=str(vc_id) if vc_id is not None else None,
                prefer_vcard_over_business=text_ad.get("PreferVCardOverBusiness") if isinstance(text_ad.get("PreferVCardOverBusiness"), str) else None,
                ad_extension_ids=text_ad.get("AdExtensions") if isinstance(text_ad.get("AdExtensions"), list) else None,
            )
            ads.append(ad_item)

            if isinstance(sl_set_id, int):
                sitelink_set_ids.add(sl_set_id)
            if isinstance(biz_id, int):
                business_ids.add(biz_id)
            if isinstance(vc_id, int):
                vcard_ids.add(vc_id)

        # Resolve sitelinks
        sitelinks_sets: list[YandexSitelinkSetItem] = []
        if sitelink_set_ids:
            try:
                sl_response = direct.sitelinks_get(ids=sorted(sitelink_set_ids))
            except YandexDirectError:
                sl_response = None
            if sl_response and sl_response.get("ok"):
                sl_result = sl_response.get("result") or {}
                for sl_set in sl_result.get("SitelinksSets") or []:
                    sl_items = [
                        YandexSitelinkItem(
                            title=str(s.get("Title") or ""),
                            href=s.get("Href") if isinstance(s.get("Href"), str) else None,
                            description=s.get("Description") if isinstance(s.get("Description"), str) else None,
                        )
                        for s in (sl_set.get("Sitelinks") or [])
                        if isinstance(s, dict)
                    ]
                    sitelinks_sets.append(
                        YandexSitelinkSetItem(
                            id=str(sl_set.get("Id") or ""),
                            sitelinks=sl_items,
                        )
                    )

        # Resolve businesses
        businesses: list[YandexBusinessAssetItem] = []
        if business_ids:
            try:
                biz_response = direct.businesses_get()
            except YandexDirectError:
                biz_response = None
            if biz_response and biz_response.get("ok"):
                biz_result = biz_response.get("result") or {}
                for b in biz_result.get("Businesses") or []:
                    if not isinstance(b, dict):
                        continue
                    b_id = b.get("Id")
                    if b_id in business_ids:
                        businesses.append(
                            YandexBusinessAssetItem(
                                id=str(b_id),
                                name=str(b.get("Name") or ""),
                                address=b.get("Address") if isinstance(b.get("Address"), str) else None,
                            )
                        )

        # Resolve vcards
        vcards: list[YandexVCardAssetItem] = []
        if vcard_ids:
            try:
                vc_response = direct.vcards_get()
            except YandexDirectError:
                vc_response = None
            if vc_response and vc_response.get("ok"):
                vc_result = vc_response.get("result") or {}
                for v in vc_result.get("VCards") or []:
                    if not isinstance(v, dict):
                        continue
                    v_id = v.get("Id")
                    if v_id in vcard_ids:
                        phone_raw = v.get("Phone")
                        phone_str: str | None = None
                        if isinstance(phone_raw, dict):
                            parts = [
                                str(phone_raw.get("CountryCode") or ""),
                                str(phone_raw.get("CityCode") or ""),
                                str(phone_raw.get("PhoneNumber") or ""),
                            ]
                            phone_str = " ".join(p for p in parts if p) or None
                        vcards.append(
                            YandexVCardAssetItem(
                                id=str(v_id),
                                company_name=str(v.get("CompanyName") or ""),
                                phone=phone_str,
                            )
                        )

        return YandexAdAssetsResult(
            campaign_id=campaign_id,
            source="yandex",
            read_only=True,
            ads=ads,
            sitelinks_sets=sitelinks_sets,
            businesses=businesses,
            vcards=vcards,
            callouts=[],
            missing=YandexAdAssetsMissing(),
        )

    # Mock mode
    mock_ads = mock_yandex.list_ads(campaign_id)
    ad_items = [
        YandexAdAssetItem(
            id=a.get("id", ""),
            ad_group_id=a.get("ad_group_id", ""),
            campaign_id=a.get("campaign_id", campaign_id),
            status=a.get("status", "active"),
            state="ON",
            type="TEXT_AD",
            title=a.get("title", ""),
            text="",
            href="",
        )
        for a in mock_ads
    ]
    return YandexAdAssetsResult(
        campaign_id=campaign_id,
        source="mock",
        read_only=True,
        ads=ad_items,
        sitelinks_sets=[],
        businesses=[],
        vcards=[],
        callouts=[],
    )


@app.get("/yandex/vcards", response_model=YandexRawResult)
def yandex_vcards(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "vcards", "get", lambda c: c.vcards_get())


@app.post("/yandex/vcards", response_model=YandexVCardResult)
def yandex_vcards_add(
    payload: YandexVCardRequest,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexVCardResult:
    try:
        return store.yandex_vcard_add(payload, settings=settings, client=client)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except YandexDirectError as exc:
        raise _yandex_error_to_502(exc) from exc


@app.post("/yandex/ads/business", response_model=YandexAdsBusinessAttachResult)
def yandex_ads_business_attach(
    payload: YandexAdsBusinessAttachRequest,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexAdsBusinessAttachResult:
    try:
        return store.yandex_ads_business_attach(payload, settings=settings, client=client)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except YandexDirectError as exc:
        message = str(exc)
        if "Live writes require" in message or "live_readonly" in message:
            raise HTTPException(status_code=409, detail=message) from exc
        raise _yandex_error_to_502(exc) from exc


@app.post(
    "/yandex/ad-groups/{ad_group_id}/ads",
    response_model=LiveAdCreateResult,
)
def yandex_ad_group_ads_add(
    ad_group_id: str,
    payload: LiveAdCreateRequest,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> LiveAdCreateResult:
    try:
        return store.yandex_ad_group_ads_add(
            ad_group_id, payload, settings=settings, client=client
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except YandexDirectError as exc:
        message = str(exc)
        if "Live writes require" in message or "live_readonly" in message:
            raise HTTPException(status_code=409, detail=message) from exc
        raise _yandex_error_to_502(exc) from exc


@app.post("/yandex/ads/moderate", response_model=AdsModerateResult)
def yandex_ads_moderate(
    payload: AdsModerateRequest,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> AdsModerateResult:
    try:
        return store.yandex_ads_moderate(payload, settings=settings, client=client)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except YandexDirectError as exc:
        message = str(exc)
        if "Live writes require" in message or "live_readonly" in message:
            raise HTTPException(status_code=409, detail=message) from exc
        raise _yandex_error_to_502(exc) from exc


@app.get("/yandex/ad-images", response_model=YandexRawResult)
def yandex_ad_images(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "adimages", "get", lambda c: c.adimages_get())


@app.get("/yandex/creatives", response_model=YandexRawResult)
def yandex_creatives(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "creatives", "get", lambda c: c.creatives_get())


@app.get("/yandex/feeds", response_model=YandexRawResult)
def yandex_feeds(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "feeds", "get", lambda c: c.feeds_get())


@app.get("/yandex/businesses", response_model=YandexRawResult)
def yandex_businesses(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "businesses", "get", lambda c: c.businesses_get())


@app.get("/yandex/agency-clients", response_model=YandexRawResult)
def yandex_agency_clients(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "agencyclients", "get", lambda c: c.agencyclients_get())


def _csv_items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _csv_ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


@app.get("/yandex/keywords-research/has-search-volume", response_model=YandexRawResult)
def yandex_keywords_has_search_volume(
    keywords: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(
        settings,
        client,
        "keywordsresearch",
        "hasSearchVolume",
        lambda c: c.keywordsresearch_has_search_volume(_csv_items(keywords)),
    )


@app.get("/yandex/keywords-research/deduplicate", response_model=YandexRawResult)
def yandex_keywords_deduplicate(
    keywords: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(
        settings,
        client,
        "keywordsresearch",
        "deduplicate",
        lambda c: c.keywordsresearch_deduplicate(_csv_items(keywords)),
    )


@app.get(
    "/yandex/keywords-research/wordstat/create",
    response_model=YandexRawResult,
    responses=YANDEX_DIRECT_ERROR_RESPONSES,
    deprecated=True,
)
def yandex_wordstat_create(
    phrases: str,
    geo_ids: str = "213",
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(
        settings,
        client,
        "keywordsresearch",
        "createNewWordstatReport",
        lambda c: c.keywordsresearch_create_wordstat_report(_csv_items(phrases), _csv_ints(geo_ids)),
    )


@app.get(
    "/yandex/keywords-research/wordstat/{report_id}",
    response_model=YandexRawResult,
    responses=YANDEX_DIRECT_ERROR_RESPONSES,
    deprecated=True,
)
def yandex_wordstat_get(
    report_id: int,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(
        settings,
        client,
        "keywordsresearch",
        "getWordstatReport",
        lambda c: c.keywordsresearch_get_wordstat_report(report_id),
    )


@app.delete(
    "/yandex/keywords-research/wordstat/{report_id}",
    response_model=YandexRawResult,
    responses=YANDEX_DIRECT_ERROR_RESPONSES,
    deprecated=True,
)
def yandex_wordstat_delete(
    report_id: int,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(
        settings,
        client,
        "keywordsresearch",
        "deleteWordstatReport",
        lambda c: c.keywordsresearch_delete_wordstat_report(report_id),
    )


@app.get("/yandex/reports/live/{report_type}", response_model=YandexRawResult)
def yandex_report(
    report_type: str,
    date_from: str,
    date_to: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(
        settings,
        client,
        "reports",
        report_type,
        lambda c: c.report(report_type, date_from=date_from, date_to=date_to),
    )


@app.get("/yandex/reports/search-queries-live", response_model=YandexRawResult)
def yandex_search_queries_live(
    date_from: str,
    date_to: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(
        settings,
        client,
        "reports",
        "SEARCH_QUERY_PERFORMANCE_REPORT",
        lambda c: c.report("SEARCH_QUERY_PERFORMANCE_REPORT", date_from=date_from, date_to=date_to),
    )

@app.get(
    "/yandex/reports/summary",
    response_model=ReportSummary,
    responses={
        409: {"description": "Non-mock mode requires configured Yandex credentials/client."},
        502: {"description": "Redacted Yandex Direct Reports API error."},
    },
)
def yandex_reports_summary(
    date_from: str | None = Query(default=None, description="ISO date (YYYY-MM-DD). Defaults to 7 days ago."),
    date_to: str | None = Query(default=None, description="ISO date (YYYY-MM-DD). Defaults to today."),
    campaign_id: str | None = Query(default=None, description="Optional Yandex Direct campaign id filter."),
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> ReportSummary:
    """Live read-only summary from CAMPAIGN_PERFORMANCE_REPORT.

    In non-mock modes (``sandbox`` / ``live_readonly`` / ``live_write``)
    with an available Yandex Direct client this calls the v5 reports
    endpoint, parses the TSV response, and aggregates spend/clicks/
    impressions across all returned rows. ``period`` reflects the
    requested date range (or the default 7-day window). ``source`` is
    ``"yandex"`` and ``read_only`` is ``True``.

    The mock payload is the fallback ONLY for ``DIRECTPILOT_MODE=mock``
    or when no Yandex client/token is available. Live mode without a
    usable client surfaces HTTP 409 (the same contract used by other
    read-only endpoints), not a silent mock — marketing must not
    mistake mock numbers for live numbers.
    """
    # Fallback path: mock mode, or live mode but no client/token.
    if settings.directpilot_mode == "mock" or client is None:
        if settings.directpilot_mode != "mock":
            # Live read mode without a usable client — be explicit
            # rather than silently returning mock data.
            raise HTTPException(
                status_code=409,
                detail=(
                    "This endpoint requires sandbox, live_readonly, or live_write "
                    "mode with Yandex credentials"
                ),
            )
        data = mock_yandex.report_summary()
        return ReportSummary(**data, source="mock")

    # Live read-only path: real CAMPAIGN_PERFORMANCE_REPORT, parsed.
    today = date.today()
    if date_to is None:
        date_to = today.isoformat()
    if date_from is None:
        date_from = (today - timedelta(days=6)).isoformat()

    period = f"{date_from}..{date_to}"

    try:
        report_kwargs: dict[str, Any] = {
            "date_from": date_from,
            "date_to": date_to,
        }
        if campaign_id is not None:
            report_kwargs["campaign_ids"] = [campaign_id]
        response = client.report("CAMPAIGN_PERFORMANCE_REPORT", **report_kwargs)
    except YandexDirectError as exc:
        raise _yandex_error_to_502(exc) from exc

    if not response.get("ok"):
        err = response.get("error") or {}
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": (
                    f"Yandex Direct rejected reports: error_code="
                    f"{err.get('error_code')!r}"
                ),
            },
        )

    # The client returns the raw TSV text in ``result``. NEVER log it
    # (it contains customer campaign data); parse and aggregate.
    tsv_text = response.get("result") or ""
    totals = _aggregate_campaign_performance_tsv(tsv_text, campaign_id=campaign_id)

    impressions = totals["impressions"]
    clicks = totals["clicks"]
    spend = totals["spend"]
    # CTR / CPC are recomputed from totals so the response is consistent
    # with the v5 column values, regardless of how Yandex formatted them.
    ctr = (clicks / impressions * 100.0) if impressions else 0.0
    cpc = (spend / clicks) if clicks else 0.0

    return ReportSummary(
        period=period,
        spend=spend,
        clicks=int(totals["clicks"]),
        impressions=int(totals["impressions"]),
        ctr=round(ctr, 4),
        cpc=round(cpc, 4),
        conversions=None,
        cpa=None,
        source="yandex",
        read_only=True,
    )


def _aggregate_campaign_performance_tsv(
    tsv_text: str, campaign_id: str | None = None
) -> dict[str, float | int]:
    """Aggregate a CAMPAIGN_PERFORMANCE_REPORT TSV into totals.

    Expected column order (matches ``YandexDirectClient.report`` defaults):
        Date, CampaignId, CampaignName, Impressions, Clicks, Cost, Ctr

    Returns a dict with ``impressions``, ``clicks``, ``spend`` summed
    across the rows. Rows whose ``CampaignId`` does not match an
    optional ``campaign_id`` filter are dropped. Malformed rows are
    skipped silently (the endpoint surfaces 502 only on transport /
    envelope errors, not on per-row parse noise).
    """
    impressions = 0
    clicks = 0
    spend = 0.0
    seen = False
    for raw_line in tsv_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cols = line.split("\t")
        # Need at least Date, CampaignId, ..., Impressions, Clicks, Cost
        # i.e. index 5 (Cost) reachable. CTR is index 6 — we ignore it
        # and recompute CTR/CPC from totals instead.
        if len(cols) < 6:
            continue
        # Skip the header row (TSV first line repeats the field names).
        if cols[0].lower() == "date":
            continue
        if campaign_id is not None and cols[1] != campaign_id:
            continue
        try:
            impressions += int(cols[3])
            clicks += int(cols[4])
            spend += float(cols[5])
        except ValueError:
            # Malformed numeric — skip the row, do not raise.
            continue
        seen = True
    # If we got a report body but nothing matched the filter, return zeros
    # rather than 502 — the report is valid, it just has no rows for the
    # requested campaign / period.
    if not seen and not tsv_text.strip():
        return {"impressions": 0, "clicks": 0, "spend": 0.0}
    return {"impressions": impressions, "clicks": clicks, "spend": spend}


# Default field set for SEARCH_QUERY_PERFORMANCE_REPORT. The order matches
# what we request from Yandex and what the parser expects:
# Query, CampaignId, AdGroupId, Impressions, Clicks, Ctr, Cost.
_SEARCH_QUERY_REPORT_FIELDS: tuple[str, ...] = (
    "Query",
    "CampaignId",
    "AdGroupId",
    "Impressions",
    "Clicks",
    "Ctr",
    "Cost",
)


def _aggregate_search_query_tsv(
    tsv_text: str, campaign_id: str | None = None
) -> list[YandexSearchQuery]:
    """Parse a SEARCH_QUERY_PERFORMANCE_REPORT TSV into YandexSearchQuery items.

    Expected column order (matches ``_SEARCH_QUERY_REPORT_FIELDS`` above):
        Query, CampaignId, AdGroupId, Impressions, Clicks, Ctr, Cost

    Rows whose ``CampaignId`` does not match the optional ``campaign_id``
    filter are dropped. Malformed rows are skipped silently (the endpoint
    surfaces 502 only on transport / envelope errors, not on per-row parse
    noise). Empty input returns an empty list — a real live report with
    no rows is a valid response, not a 502 and not a mock fallback.
    """
    items: list[YandexSearchQuery] = []
    for raw_line in tsv_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cols = line.split("\t")
        # Need at least Query, CampaignId, AdGroupId, Impressions, Clicks, Ctr, Cost
        # i.e. index 6 (Cost) reachable.
        if len(cols) < 7:
            continue
        # Skip the header row (TSV first line repeats the field names).
        if cols[0].lower() == "query":
            continue
        if campaign_id is not None and cols[1] != campaign_id:
            continue
        try:
            impressions = int(cols[3])
            clicks = int(cols[4])
            ctr = float(cols[5])
        except ValueError:
            # Malformed numeric — skip the row, do not raise.
            continue
        items.append(
            YandexSearchQuery(
                query=cols[0],
                impressions=impressions,
                clicks=clicks,
                ctr=round(ctr, 4),
            )
        )
    return items


@app.get(
    "/yandex/reports/search-queries",
    response_model=YandexSearchQueriesReport,
    responses={
        409: {"description": "Non-mock mode requires configured Yandex credentials/client."},
        502: {"description": "Redacted Yandex Direct Reports API error."},
    },
)
def yandex_search_queries(
    date_from: str | None = Query(default=None, description="ISO date (YYYY-MM-DD). Defaults to 7 days ago."),
    date_to: str | None = Query(default=None, description="ISO date (YYYY-MM-DD). Defaults to today."),
    campaign_id: str | None = Query(default=None, description="Optional Yandex Direct campaign id filter."),
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexSearchQueriesReport:
    """Search query performance for the requested period.

    In ``DIRECTPILOT_MODE=mock`` the deterministic mock payload is returned
    (with ``source="mock"``). In any non-mock mode (``sandbox`` /
    ``live_readonly`` / ``live_write``) with a configured Yandex Direct
    client, the live ``SEARCH_QUERY_PERFORMANCE_REPORT`` v5 reports
    endpoint is called and the TSV is parsed into YandexSearchQuery items
    with ``source="yandex"``, ``read_only=True``. An empty live report is
    a valid response — it returns ``items=[]`` and ``source="yandex"``,
    not a mock fallback and not a 502.

    When the live mode is selected but no client/token is available the
    endpoint surfaces HTTP 409 (same contract as
    ``/yandex/reports/summary`` and the other read-only endpoints), not
    a silent mock — marketing must not mistake mock numbers for live
    numbers.
    """
    # Fallback path: mock mode, or live mode but no client/token.
    if settings.directpilot_mode == "mock" or client is None:
        if settings.directpilot_mode != "mock":
            # Live read mode without a usable client — be explicit
            # rather than silently returning mock data.
            raise HTTPException(
                status_code=409,
                detail=(
                    "This endpoint requires sandbox, live_readonly, or live_write "
                    "mode with Yandex credentials"
                ),
            )
        items = [YandexSearchQuery(**q) for q in mock_yandex.search_queries()]
        return YandexSearchQueriesReport(
            period="last_7_days",
            items=items,
            source="mock",
            read_only=True,
        )

    # Live read-only path: real SEARCH_QUERY_PERFORMANCE_REPORT, parsed.
    today = date.today()
    if date_to is None:
        date_to = today.isoformat()
    if date_from is None:
        date_from = (today - timedelta(days=6)).isoformat()

    period = f"{date_from}..{date_to}"

    try:
        report_kwargs: dict[str, Any] = {
            "date_from": date_from,
            "date_to": date_to,
            "field_names": list(_SEARCH_QUERY_REPORT_FIELDS),
        }
        if campaign_id is not None:
            report_kwargs["campaign_ids"] = [campaign_id]
        response = client.report(
            "SEARCH_QUERY_PERFORMANCE_REPORT", **report_kwargs
        )
    except YandexDirectError as exc:
        raise _yandex_error_to_502(exc) from exc

    if not response.get("ok"):
        err = response.get("error") or {}
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": (
                    f"Yandex Direct rejected reports: error_code="
                    f"{err.get('error_code')!r}"
                ),
            },
        )

    # The client returns the raw TSV text in ``result``. NEVER log it
    # (it contains customer search query data); parse and aggregate.
    tsv_text = response.get("result") or ""
    items = _aggregate_search_query_tsv(tsv_text, campaign_id=campaign_id)

    return YandexSearchQueriesReport(
        period=period,
        items=items,
        source="yandex",
        read_only=True,
    )


# ---------------------------------------------------------------------------
# Yandex Direct control facade (pause / resume)
# ---------------------------------------------------------------------------


@app.post(
    "/yandex/campaigns/{campaign_id}/pause",
    response_model=YandexControlResult,
)
def yandex_pause(
    campaign_id: str,
    payload: YandexControlRequest,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexControlResult:
    if not payload.approved:
        raise HTTPException(status_code=409, detail="Action requires explicit approval")
    if settings.directpilot_mode == "live_readonly" and not payload.dry_run:
        raise HTTPException(
            status_code=409,
            detail="Live writes require DIRECTPILOT_MODE=live_write; live_readonly only allows dry_run",
        )
    try:
        return store.yandex_control(
            campaign_id, "pause", payload, settings=settings, client=client
        )
    except YandexDirectError as exc:
        # Never include the OAuth token in the response.
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": str(exc),
            },
        ) from exc


@app.post(
    "/yandex/campaigns/{campaign_id}/resume",
    response_model=YandexControlResult,
)
def yandex_resume(
    campaign_id: str,
    payload: YandexControlRequest,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexControlResult:
    if not payload.approved:
        raise HTTPException(status_code=409, detail="Action requires explicit approval")
    if settings.directpilot_mode == "live_readonly" and not payload.dry_run:
        raise HTTPException(
            status_code=409,
            detail="Live writes require DIRECTPILOT_MODE=live_write; live_readonly only allows dry_run",
        )
    try:
        return store.yandex_control(
            campaign_id, "resume", payload, settings=settings, client=client
        )
    except YandexDirectError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": str(exc),
            },
        ) from exc


# ---------------------------------------------------------------------------
# Yandex AI Studio / Search API v2 — Wordstat
#
# This is the modern documented v2 path (https://yandex.cloud/en/services/
# search-api) and is fully separate from the v5 keywordsresearch service
# used by the rest of the /yandex/* facade. Wordstat on v5 is not
# implemented, which is why the older keywordsresearch.wordstat.* helpers
# return UNSUPPORTED_IN_V5 envelopes.
# ---------------------------------------------------------------------------


def get_yandex_search_wordstat_client(
    settings: Settings = Depends(get_settings),
) -> YandexSearchWordstatClient:
    """Build a YandexSearchWordstatClient for the v2 Wordstat endpoints.

    The client is always created — even when no API key is configured.
    Missing-key errors are raised inside the client's ``_post`` method so
    the corresponding /wordstat/* endpoints can translate them into a 503
    "service not configured" response without crashing.
    """
    return YandexSearchWordstatClient(settings=settings)


def _wordstat_error_to_503(exc: YandexSearchWordstatError) -> HTTPException:
    """Translate a missing-config error into a 503 (service not configured)."""
    return HTTPException(
        status_code=503,
        detail={
            "error_type": "YandexSearchWordstatError",
            "message": str(exc),
        },
    )


def _wordstat_error_to_502(exc: YandexSearchWordstatError) -> HTTPException:
    """Translate an upstream / transport error into a 502 with no key echo."""
    return HTTPException(
        status_code=502,
        detail={
            "error_type": "YandexSearchWordstatError",
            "message": str(exc),
        },
    )


def _parse_int_list(raw: list[str] | None) -> list[int] | None:
    """Parse repeated and/or CSV query params into ints.

    Supports both ``?regions=43&regions=213`` and ``?regions=43,213``.
    ``None`` is returned for an empty list so callers can keep the "omit
    when not provided" semantics intact.
    """
    if not raw:
        return None
    values: list[int] = []
    for item in raw:
        for part in item.split(","):
            part = part.strip()
            if part:
                try:
                    values.append(int(part))
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"regions must contain integer ids; got {part!r}",
                    ) from exc
    return values or None


def _raise_wordstat_http_error(exc: YandexSearchWordstatError) -> None:
    if isinstance(exc, YandexSearchWordstatMissingKeyError):
        raise _wordstat_error_to_503(exc) from exc
    raise _wordstat_error_to_502(exc) from exc


@app.get(
    "/wordstat/top",
    response_model=YandexSearchApiResult,
    responses=WORDSTAT_ERROR_RESPONSES,
)
def wordstat_top(
    phrase: str,
    regions: list[str] | None = Query(default=None),
    limit: int | None = None,
    devices: list[str] | None = Query(default=None),
    client: YandexSearchWordstatClient = Depends(get_yandex_search_wordstat_client),
) -> YandexSearchApiResult:
    """Top related queries for a phrase (Yandex Search API v2 topRequests)."""
    try:
        result = client.wordstat_top_requests(
            phrase,
            region_ids=_parse_int_list(regions),
            limit=limit,
            devices=devices or None,
        )
    except YandexSearchWordstatError as exc:
        _raise_wordstat_http_error(exc)
    return YandexSearchApiResult(
        method="topRequests",
        data=result["data"],
    )


@app.get(
    "/wordstat/dynamics",
    response_model=YandexSearchApiResult,
    responses=WORDSTAT_ERROR_RESPONSES,
)
def wordstat_dynamics(
    phrase: str,
    date_from: str,
    period: str = "PERIOD_MONTHLY",
    date_to: str | None = None,
    regions: list[str] | None = Query(default=None),
    devices: list[str] | None = Query(default=None),
    client: YandexSearchWordstatClient = Depends(get_yandex_search_wordstat_client),
) -> YandexSearchApiResult:
    """Show / abs show per period (Yandex Search API v2 dynamics)."""
    try:
        result = client.wordstat_dynamics(
            phrase,
            period=period,
            date_from=date_from,
            date_to=date_to,
            region_ids=_parse_int_list(regions),
            devices=devices or None,
        )
    except YandexSearchWordstatError as exc:
        _raise_wordstat_http_error(exc)
    return YandexSearchApiResult(
        method="dynamics",
        data=result["data"],
    )


@app.get(
    "/wordstat/regions",
    response_model=YandexSearchApiResult,
    responses=WORDSTAT_ERROR_RESPONSES,
)
def wordstat_regions(
    phrase: str,
    region: str = "REGION_ALL",
    devices: list[str] | None = Query(default=None),
    client: YandexSearchWordstatClient = Depends(get_yandex_search_wordstat_client),
) -> YandexSearchApiResult:
    """Share of impressions by region (Yandex Search API v2 regions)."""
    try:
        result = client.wordstat_regions_distribution(
            phrase,
            region=region,
            devices=devices or None,
        )
    except YandexSearchWordstatError as exc:
        _raise_wordstat_http_error(exc)
    return YandexSearchApiResult(
        method="regions",
        data=result["data"],
    )


@app.get(
    "/wordstat/regions-tree",
    response_model=YandexSearchApiResult,
    responses=WORDSTAT_ERROR_RESPONSES,
)
def wordstat_regions_tree(
    client: YandexSearchWordstatClient = Depends(get_yandex_search_wordstat_client),
) -> YandexSearchApiResult:
    """Region tree (Yandex Search API v2 getRegionsTree, no phrase)."""
    try:
        result = client.wordstat_regions_tree()
    except YandexSearchWordstatError as exc:
        _raise_wordstat_http_error(exc)
    return YandexSearchApiResult(
        method="getRegionsTree",
        data=result["data"],
    )


# ---------------------------------------------------------------------------
# Yandex Direct Live v4 — account balance (read-only)
#
# Live v4 AccountManagement → Get is the canonical way to read the
# current account balance (Amount, AmountAvailableForTransfer, Currency,
# AccountDayBudget). The endpoint is read-only and never returns the
# token in any body. Without a configured `?login=` we fall back to
# `clients.get` to discover the login the current token is bound to.
# ---------------------------------------------------------------------------


def _parse_live_v4_account_block(block: Any, login: str | None) -> YandexAccountBalance:
    if not isinstance(block, dict):
        return YandexAccountBalance(login=login, raw={"value": block} if not isinstance(block, dict) else None)

    def _float_or_zero(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.replace(",", "."))
            except ValueError:
                return 0.0
        return 0.0

    day_budget = block.get("AccountDayBudget")
    day_budget_amount: float | None = None
    day_budget_mode: str | None = None
    if isinstance(day_budget, dict):
        raw_amount = day_budget.get("Amount")
        if isinstance(raw_amount, (int, float)):
            day_budget_amount = float(raw_amount)
        elif isinstance(raw_amount, str):
            try:
                day_budget_amount = float(raw_amount)
            except ValueError:
                day_budget_amount = None
        mode = day_budget.get("SpendMode")
        if isinstance(mode, str):
            day_budget_mode = mode
    amount_raw = block.get("Amount")
    available_raw = block.get("AmountAvailableForTransfer")
    return YandexAccountBalance(
        login=str(block.get("Login") or login) if block.get("Login") or login else None,
        amount=_float_or_zero(amount_raw),
        amount_available_for_transfer=_float_or_zero(available_raw),
        currency=str(block.get("Currency")) if isinstance(block.get("Currency"), str) else None,
        account_day_budget_amount=day_budget_amount,
        account_day_budget_spend_mode=day_budget_mode,
        raw=block,
    )


def _resolve_login_for_balance(
    client: YandexDirectClient,
) -> str | None:
    """Discover the login the current OAUTH token is bound to via clients.get.

    Returns ``None`` if the call fails or returns an unexpected envelope —
    the caller then surfaces the underlying 502 to the user.
    """
    try:
        response = client.clients_get()
    except YandexDirectError:
        return None
    if not response.get("ok"):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    clients = result.get("Clients") or result.get("clients") or []
    if not clients:
        return None
    first = clients[0]
    if not isinstance(first, dict):
        return None
    login = first.get("Login") or first.get("login")
    return str(login) if isinstance(login, str) and login else None


def _require_direct_read_client(
    settings: Settings, client: YandexDirectClient | None
) -> YandexDirectClient:
    if not _is_live_read_mode(settings) or client is None:
        raise HTTPException(
            status_code=409,
            detail="This endpoint requires sandbox, live_readonly, or live_write mode with Yandex credentials",
        )
    return client


@app.get(
    "/yandex/account/balance",
    response_model=YandexAccountBalanceResult,
    responses={
        502: {"model": ApiErrorResponse, "description": "Yandex Direct upstream error"},
        503: {"model": ApiErrorResponse, "description": "YANDEX_OAUTH_TOKEN is not configured"},
    },
)
def yandex_account_balance(
    login: str | None = None,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexAccountBalanceResult:
    """Read-only Live v4 AccountManagement → Get.

    Without ``?login=`` we discover the login via ``clients.get`` and then
    call Live v4. With ``?login=`` we call Live v4 directly. The token is
    never echoed back in any body.
    """
    direct = _require_direct_read_client(settings, client)
    if not direct.settings.yandex_oauth_token:
        raise HTTPException(
            status_code=503,
            detail={
                "error_type": "YandexDirectError",
                "message": "YANDEX_OAUTH_TOKEN is required for Yandex Direct API calls",
            },
        )
    if not login:
        login = _resolve_login_for_balance(direct)
    try:
        response = direct.account_balance(login=login)
    except YandexDirectError as exc:
        raise _yandex_error_to_502(exc) from exc
    if not response.get("ok"):
        err = response.get("error") or {}
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": (
                    f"Yandex Direct Live v4 rejected AccountManagement: "
                    f"error_code={err.get('error_code')!r}"
                ),
            },
        )
    data = response.get("data") or []
    accounts = [_parse_live_v4_account_block(block, login) for block in data]
    return YandexAccountBalanceResult(accounts=accounts, source="yandex", read_only=True)


# ---------------------------------------------------------------------------
# Yandex Direct campaign finance (v5 campaigns.get with finance fields)
# ---------------------------------------------------------------------------


@app.get(
    "/yandex/campaigns/finance",
    response_model=YandexCampaignFinanceList,
    responses=YANDEX_DIRECT_ERROR_RESPONSES,
)
def yandex_campaigns_finance(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexCampaignFinanceList:
    """v5 campaigns.get with Funds / Statistics / StartDate / EndDate.

    Surfaces the raw micro-unit values and the display floats for money
    fields so the caller can pick whichever representation they need.
    """
    direct = _require_direct_read_client(settings, client)
    if not direct.settings.yandex_oauth_token:
        raise HTTPException(
            status_code=503,
            detail={
                "error_type": "YandexDirectError",
                "message": "YANDEX_OAUTH_TOKEN is required for Yandex Direct API calls",
            },
        )
    try:
        response = direct.campaigns_get_finance()
    except YandexDirectError as exc:
        raise _yandex_error_to_502(exc) from exc
    if not response.get("ok"):
        err = response.get("error") or {}
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": (
                    f"Yandex Direct rejected campaigns.get (finance): "
                    f"error_code={err.get('error_code')!r}"
                ),
            },
        )
    items = [YandexCampaignFinance(**row) for row in (response.get("data") or [])]
    return YandexCampaignFinanceList(items=items, source="yandex", read_only=True)


# ---------------------------------------------------------------------------
# Semantic change package (staged / dry-run-first)
#
# Lets the user design a negative-keyword and/or positive-keyword
# change for a real Yandex Direct campaign (e.g. ``710382063``) and
# preview the exact Direct API v5 request bodies that WOULD be sent.
# Apply is gated by ``approved`` / ``idempotency_key`` / ``dry_run``
# and the runtime mode (``live_readonly`` blocks real apply;
# ``live_write`` allows it).
# ---------------------------------------------------------------------------


@app.post(
    "/campaigns/{campaign_id}/semantic-changes",
    response_model=SemanticChangePackage,
)
def prepare_semantic_change(
    campaign_id: str,
    payload: SemanticChangeRequest,
    settings: Settings = Depends(get_settings),
) -> SemanticChangePackage:
    """Build a staged semantic-change package.

    Always pure-local: no network call, no approval required. The
    response is a :class:`SemanticChangePackage` whose ``preview``
    lists the v5 ``keywords.add`` / ``adgroups.update`` operations
    that *would* be sent on apply. The user (or another tool) can
    inspect the proposed change before deciding to actually apply it.

    The Direct API v5 ``keywords.add`` method requires ``AdGroupId``
    per keyword and ``adgroups.update`` requires the target group
    ``Id``. If the user supplies either keyword list without an
    ``ad_group_id`` the request is rejected with HTTP 400 BEFORE any
    package is built.
    """
    try:
        return store.prepare_semantic_change_package(
            campaign_id, payload, settings=settings
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/semantic-changes/{package_id}/apply",
    response_model=SemanticChangeApplyResult,
)
def apply_semantic_change(
    package_id: str,
    payload: SemanticChangeApplyRequest,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> SemanticChangeApplyResult:
    """Apply a previously prepared semantic change.

    Gate contract (matches the rest of the product):

    * ``approved`` must be ``True`` — otherwise 409.
    * ``idempotency_key`` must be supplied (length >= 6) — same key
      returns the cached result without re-sending.
    * In ``live_readonly`` mode, ``dry_run=False`` is REJECTED before
      any network call.
    * In ``live_write`` mode with all gates satisfied, the operations
      from the package are sent to Yandex via the injected client.
    """
    if not payload.approved:
        raise HTTPException(
            status_code=409,
            detail="Action requires explicit approval before apply",
        )
    if settings.directpilot_mode == "live_readonly" and not payload.dry_run:
        raise HTTPException(
            status_code=409,
            detail=(
                "Live writes require DIRECTPILOT_MODE=live_write; "
                "live_readonly only allows dry_run"
            ),
        )
    try:
        return store.apply_semantic_change(
            package_id, payload, settings=settings, client=client
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="semantic change package not found"
        ) from exc
    except YandexDirectError as exc:
        # Never include the OAuth token in the response.
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:  # noqa: BLE001 — safety net
        # Last-resort safety net: even a non-typed exception from the
        # store layer (e.g. a stale cache hit, a programming bug, or an
        # unhandled httpx edge case) must be translated to 502 with a
        # redacted message. The store already records a
        # ``semantic_change_apply_failed`` audit event for typed errors;
        # we add one here too so the operator can correlate the 502.
        try:
            store.append_audit(
                "semantic_change_apply_failed",
                str(package_id),
                dry_run=False,
                details={
                    "package_id": package_id,
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "endpoint_safety_net": True,
                    "yandex_error": (
                        f"unexpected error in endpoint: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "exception_type": type(exc).__name__,
                },
            )
        except Exception:  # noqa: BLE001
            # Audit is best-effort; never let it block the safe 502.
            pass
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": (
                    f"unexpected error during semantic-change apply: "
                    f"{type(exc).__name__}"
                ),
            },
        ) from exc


# ---------------------------------------------------------------------------
# Yandex Direct live-create campaign
#
# ``POST /yandex/campaigns/live-create`` creates a real Yandex Direct
# campaign from an existing :class:`CampaignDraft` preview. The apply
# path chains ``campaigns.add`` → ``adgroups.add`` → ``ads.add`` →
# ``keywords.add``. ``negativekeywordsharedsets.add`` remains explicit
# ``not_implemented``; group-level negatives are sent through
# ``adgroups.add`` ``NegativeKeywords.Items``. Each stage is its own v5
# call so a single failure stops the chain before the next stage.
# ---------------------------------------------------------------------------


@app.post(
    "/yandex/campaigns/live-create",
    response_model=LiveCreateCampaignResult,
)
def yandex_live_create_campaign(
    payload: LiveCreateCampaignRequest,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> LiveCreateCampaignResult:
    """Create a real Yandex Direct campaign from a draft preview.

    Gates (mirrors the rest of the product):

    * ``approved`` must be ``True`` (HTTP 409 otherwise).
    * ``idempotency_key`` is required.
    * ``live_readonly`` + ``dry_run=False`` is REJECTED before any
      network call (HTTP 409).
    * ``live_write`` + ``approved`` + ``idempotency_key`` + ``dry_run=False``
      performs the real chain: ``campaigns.add`` → ``adgroups.add`` →
      ``ads.add`` → ``keywords.add``. ``negativekeywordsharedsets.add``
      remains in ``not_implemented``; group-level negatives are sent via
      ``adgroups.add`` ``NegativeKeywords.Items`` (block is OPTIONAL —
      omitted when the draft has no negatives, included with the items
      when it does). The chain does not auto-activate or call
      ``campaigns.resume``; activation/moderation handoff stays a
      separate approved step.

    Region / geo targeting:

    * ``adgroups.add`` items ALWAYS carry ``RegionIds`` (v5 rejects
      items without a geo target). The ids are resolved from
      ``draft.region`` via the explicit local map
      ``_REGION_NAME_TO_V5_IDS`` in ``app/store.py`` (helper
      ``_resolve_region_to_ids``). No external lookup, no network
      call. Supported region names in the Beta: ``Казань`` → ``[43]``,
      ``Москва`` → ``[213]``, ``Санкт-Петербург`` / ``СПб`` → ``[2]``,
      ``Россия`` / ``Russia`` → ``[225]``. Trivially extensible.
    * An unmapped / empty / whitespace region fails closed BEFORE any
      ``campaigns.add`` network call. The chain raises
      :class:`YandexDirectError` with a redacted message that names
      the offending region, the public store method audits
      ``live_create_campaign_failed`` (no token in the audit), and
      the endpoint returns HTTP 502. The dry-run preview surfaces the
      same failure so the operator sees the same mode in both paths.
    * The ``adgroups.add`` payload does NOT carry a ``Status`` field —
      lifecycle/moderation state is controlled by Direct and the
      separate resume endpoint, not by the create chain.
    """
    if not payload.approved:
        raise HTTPException(
            status_code=409,
            detail="Action requires explicit approval before live-create",
        )
    # Mode gate: only ``live_write`` may perform a real apply. The
    # other three modes (``mock`` / ``sandbox`` / ``live_readonly``)
    # are REJECTED before any network call so the rejection is
    # guaranteed to be no-network. ``sandbox`` shares the v5
    # ``campaigns.add`` write shape with production — a real apply
    # against a sandbox token would create a real campaign on the
    # user's sandbox account, which is the same shape of
    # misconfiguration we are protecting against. ``mock`` has no
    # live client at all — silently returning ``applied=False`` (a
    # dry-run shape) would lie to the operator. ``live_readonly`` is
    # the documented read-only path. ``dry_run=True`` short-circuits
    # all of this and is allowed in every mode.
    if not payload.dry_run and settings.directpilot_mode != "live_write":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Live writes require DIRECTPILOT_MODE=live_write; "
                f"current mode is {settings.directpilot_mode!r}; "
                f"live-create apply is not allowed in this mode "
                f"(dry_run=True is the only allowed path)"
            ),
        )
    try:
        return store.live_create_campaign(
            payload, settings=settings, client=client
        )
    except ValueError as exc:
        # The store raises ``ValueError`` for in-store gate
        # violations (e.g. unapproved apply if the endpoint gate is
        # bypassed by a direct caller). Surface as 409 with the
        # reason — never as the opaque FastAPI 500 default. A
        # ``ValueError`` from anywhere else would also be caught by
        # the generic ``Exception`` safety net below; this explicit
        # branch ensures the gate-violation case is NEVER mis-coded
        # as 502.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="Campaign draft not found"
        ) from exc
    except YandexDirectError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:  # noqa: BLE001 — safety net
        # Same last-resort contract as the semantic-change endpoint:
        # any non-typed exception becomes 502 with a redacted message.
        try:
            store.append_audit(
                "live_create_campaign_failed",
                payload.draft_id,
                dry_run=False,
                details={
                    "draft_id": payload.draft_id,
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "endpoint_safety_net": True,
                    "yandex_error": (
                        f"unexpected error in live-create endpoint: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "exception_type": type(exc).__name__,
                },
            )
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": (
                    f"unexpected error during live-create: "
                    f"{type(exc).__name__}"
                ),
            },
        ) from exc


# ---------------------------------------------------------------------------
# Yandex Direct campaign TimeTargeting read (GET)
#
# ``GET /yandex/campaigns/{campaign_id}/time-targeting`` reads the current
# ``TimeTargeting`` block from the campaign via v5 ``campaigns.get``
# with the ``TimeTargeting`` field set. No write gate — this is a
# pure read-only endpoint available in ``mock``, ``sandbox``,
# ``live_readonly``, and ``live_write``. No ``approved``, no
# ``idempotency_key``, no network write call.
# ---------------------------------------------------------------------------


def _parse_v5_time_targeting_to_schedule(
    time_targeting: dict,
) -> YandexTimeTargetingSchedule | None:
    """Parse a v5 ``TimeTargeting`` block into a normalized schedule.

    The v5 shape is ``{Schedule: {Items: [str, ...]}, ...}`` where
    each item is ``"daynum,percent0,percent1,...,percent23"``.
    Returns ``None`` if the shape is unparseable.
    """
    try:
        schedule_block = time_targeting.get("Schedule", {})
        if not isinstance(schedule_block, dict):
            return None
        items = schedule_block.get("Items")
        if not isinstance(items, list) or len(items) != 7:
            return None
        days: list[YandexTimeTargetingHourly] = []
        for item in items:
            if not isinstance(item, str):
                return None
            parts = item.split(",")
            if len(parts) != 25:  # daynum + 24 percents
                return None
            try:
                hours = [int(p) for p in parts[1:]]
            except (ValueError, TypeError):
                return None
            if len(hours) != 24:
                return None
            # Validate range — out-of-range values mean unparseable.
            if any(h < 0 or h > 100 for h in hours):
                return None
            days.append(YandexTimeTargetingHourly(hours=hours))
        return YandexTimeTargetingSchedule(days=days)
    except Exception:
        return None


@app.get(
    "/yandex/campaigns/{campaign_id}/time-targeting",
    response_model=YandexTimeTargetingReadResult,
)
def yandex_time_targeting_read(
    campaign_id: str,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexTimeTargetingReadResult:
    """Read the current TimeTargeting / hourly schedule of a campaign.

    Pure read-only — no ``approved``, no ``idempotency_key``, no
    network write call. Available in all modes.

    - **mock**: returns a deterministic schedule with
      ``source="mock"``.
    - **live** (sandbox / live_readonly / live_write): calls
      ``campaigns_get_time_targeting`` (v5 ``campaigns.get`` with
      ``TimeTargeting`` field) and returns the raw ``TimeTargeting``
      block plus a normalized 7×24 ``schedule``.
    - Upstream Yandex errors are redacted (no token leakage) and
      surfaced as 502.
    """
    if _is_live_read_mode(settings) and client is not None:
        try:
            response = client.campaigns_get_time_targeting(campaign_id)
        except YandexDirectError as exc:
            raise _yandex_error_to_502(exc) from exc
        if not response.get("ok"):
            err = response.get("error") or {}
            raise HTTPException(
                status_code=502,
                detail={
                    "error_type": "YandexDirectError",
                    "message": (
                        "Yandex Direct rejected campaigns.get (TimeTargeting): "
                        f"error_code={err.get('error_code')!r}"
                    ),
                },
            )
        result = response.get("result") or {}
        campaigns = result.get("Campaigns") if isinstance(result, dict) else None
        if isinstance(campaigns, list) and campaigns and isinstance(campaigns[0], dict):
            camp = campaigns[0]
            raw_tt = camp.get("TimeTargeting")
            campaign_name = camp.get("Name")
        else:
            raw_tt = None
            campaign_name = None
        schedule = None
        if isinstance(raw_tt, dict):
            schedule = _parse_v5_time_targeting_to_schedule(raw_tt)
            raw_tt = dict(raw_tt)  # defensive copy
        return YandexTimeTargetingReadResult(
            campaign_id=campaign_id,
            campaign_name=str(campaign_name) if campaign_name else None,
            source="yandex",
            read_only=True,
            time_targeting=raw_tt if isinstance(raw_tt, dict) else None,
            schedule=schedule,
        )

    # Mock mode (or no client): deterministic local data.
    mock = mock_yandex.mock_time_targeting(campaign_id)
    raw_tt = mock.get("time_targeting")
    schedule = None
    if isinstance(raw_tt, dict):
        schedule = _parse_v5_time_targeting_to_schedule(raw_tt)
    return YandexTimeTargetingReadResult(
        campaign_id=campaign_id,
        campaign_name=mock.get("campaign_name"),
        source="mock",
        read_only=True,
        time_targeting=raw_tt if isinstance(raw_tt, dict) else None,
        schedule=schedule,
    )


# ---------------------------------------------------------------------------
# Yandex Direct campaign TimeTargeting update
#
# ``POST /yandex/campaigns/{campaign_id}/time-targeting`` updates the
# hourly-bidding schedule (TimeTargeting) of an existing Yandex
# Direct campaign via v5 ``campaigns.update``. The gate contract is
# identical to the rest of the product surface:
#
# * ``approved`` must be ``True`` (HTTP 409 otherwise).
# * ``idempotency_key`` is required.
# * ``live_readonly`` + ``dry_run=False`` is REJECTED before any
#   network call (HTTP 409).
# * ``live_write`` + ``approved`` + ``idempotency_key`` +
#   ``dry_run=False`` performs the real apply: a v5
#   ``campaigns.update`` call with the canonical ``TimeTargeting``
#   block, followed by a read-back via ``campaigns.get`` to verify
#   the schedule landed. ``sandbox`` is rejected (same write shape
#   as production, so the gate is strict).
#
# The request body accepts the schedule in one of two shapes (see
# ``YandexTimeTargetingRequest``): the full 7 x 24 ``schedule``
# matrix, or the flat ``hours`` list plus an optional ``days``
# filter. The endpoint normalises both shapes into the canonical
# v5 day-of-week order MONDAY..SUNDAY before sending.
#
# Direct v5 ``campaigns.update`` is a REPLACE-shaped call for the
# ``TimeTargeting`` block — sending the new block atomically
# replaces the previous schedule. Other campaign fields are not
# included in the payload so the apply touches only the schedule.
# ---------------------------------------------------------------------------


@app.post(
    "/yandex/campaigns/{campaign_id}/time-targeting",
    response_model=YandexTimeTargetingResult,
)
def yandex_time_targeting(
    campaign_id: str,
    payload: YandexTimeTargetingRequest,
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexTimeTargetingResult:
    """Update the TimeTargeting / hourly schedule of a campaign.

    Gates (mirrors the rest of the product):

    * ``approved`` must be ``True`` (HTTP 409 otherwise).
    * ``idempotency_key`` is required (HTTP 422 otherwise — Pydantic).
    * ``live_readonly`` + ``dry_run=False`` is REJECTED before any
      network call (HTTP 409). The dry-run path is allowed in every
      mode and never mutates.
    * ``live_write`` + ``approved`` + ``idempotency_key`` +
      ``dry_run=False`` performs the real apply: v5
      ``campaigns.update`` with the canonical ``TimeTargeting``
      block, followed by a read-back via v5 ``campaigns.get
      TimeTargeting`` to verify the schedule landed. The response
      surfaces the read-back so the operator can diff it against
      ``schedule_applied`` without re-querying.

    The request body accepts the schedule in one of two shapes
    (see :class:`YandexTimeTargetingRequest`):

    1. ``schedule`` — the full 7 x 24 matrix (positional, in the
       v5 day-of-week order MONDAY..SUNDAY).
    2. ``hours`` — a flat 24-value list (0..100) plus an optional
       ``days`` filter (``["MONDAY", ..., "SUNDAY"]``). Convenience
       for the common "use these hours every day" use case; the
       endpoint expands it into the canonical 7 x 24 matrix. Days
       not listed in ``days`` are set to all-zeros (paused) on the
       apply so the operator sees an explicit zero schedule on the
       missing days, not a silent carry-over of the previous
       schedule.

    Either ``schedule`` or ``hours`` MUST be supplied; supplying
    both is a 422 validation error.

    Audit events ``yandex_time_targeting_requested`` (every
    request, dry-run or apply) and ``yandex_time_targeting_failed``
    (only on apply-path failure) record the request id, the
    schedule, the timezone label, and the Yandex error (no token
    in the audit). Mock mode does NOT call any client method —
    the apply is a pure in-memory mirror with a deterministic
    ``readback`` shape so the operator can preview the v5 payload
    the apply would send.
    """
    if not payload.approved:
        raise HTTPException(
            status_code=409,
            detail="Action requires explicit approval before time-targeting update",
        )
    if not payload.dry_run and settings.directpilot_mode != "live_write":
        # Mode gate: only ``live_write`` may perform a real apply.
        # ``live_readonly`` and ``sandbox`` are REJECTED before any
        # network call. ``sandbox`` shares the v5
        # ``campaigns.update`` write shape with production; a
        # sandbox-apply would mutate the user's sandbox account.
        # ``mock`` has no live client — silently returning
        # ``applied=False`` (a dry-run shape) would lie to the
        # operator. ``dry_run=True`` short-circuits all of this
        # and is allowed in every mode.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Live writes require DIRECTPILOT_MODE=live_write; "
                f"current mode is {settings.directpilot_mode!r}; "
                f"time-targeting apply is not allowed in this mode "
                f"(dry_run=True is the only allowed path)"
            ),
        )
    try:
        return store.yandex_time_targeting(
            campaign_id, payload, settings=settings, client=client
        )
    except ValueError as exc:
        # ``ValueError`` is the in-store gate violation signal
        # (e.g. unapproved apply when the endpoint gate is
        # bypassed). Surface as 409 with the reason — never as
        # the opaque FastAPI 500 default.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except YandexDirectError as exc:
        # Two cases land here:
        #
        # 1. ``live_readonly`` / ``sandbox`` apply pre-flight gate
        #    (the endpoint gate is the primary; this is a
        #    defence-in-depth check from the store).
        # 2. The apply path's v5 ``campaigns.update`` rejection.
        #    The store audits ``yandex_time_targeting_failed``
        #    before re-raising, so the audit log already carries
        #    the failing stage and the redacted Yandex error.
        # Both surface as 502 with a typed envelope.
        diagnostics = exc.diagnostics or {}
        detail: dict[str, Any] = {
            "error_type": "YandexDirectError",
            "message": str(exc),
        }
        if "error_code" in diagnostics:
            detail["error_code"] = diagnostics["error_code"]
        if "error_detail" in diagnostics:
            detail["error_detail"] = diagnostics["error_detail"]
        if "payload_preview" in diagnostics:
            detail["payload_preview"] = diagnostics["payload_preview"]
        raise HTTPException(status_code=502, detail=detail) from exc
    except Exception as exc:  # noqa: BLE001 — safety net
        # Last-resort contract: any non-typed exception becomes
        # 502 with a redacted message. The token is never
        # included.
        try:
            store.append_audit(
                "yandex_time_targeting_failed",
                campaign_id,
                dry_run=False,
                details={
                    "campaign_id": campaign_id,
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "endpoint_safety_net": True,
                    "yandex_error": (
                        f"unexpected error in time-targeting endpoint: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "exception_type": type(exc).__name__,
                },
            )
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "YandexDirectError",
                "message": (
                    f"unexpected error during time-targeting: "
                    f"{type(exc).__name__}"
                ),
            },
        ) from exc


# ---------------------------------------------------------------------------
# Yandex Metrika — read-only (counters, goals, summary, traffic-sources)
#
# The Metrika Management API (api-metrika.yandex.net/management/v1) and
# the Stats API (api-metrika.yandex.net/stat/v1) are separate from the
# v5 Direct API. They use a service OAUTH token (NOT an Api-Key, NOT a
# v5 OAuth token) and `Authorization: OAuth <token>` for auth.
# ---------------------------------------------------------------------------


def get_yandex_metrika_client(
    settings: Settings = Depends(get_settings),
) -> YandexMetrikaClient:
    """Build a YandexMetrikaClient for the read-only Metrika endpoints.

    The client is always created — even when no OAUTH token is configured.
    Missing-token errors are raised inside the client's request methods so
    the /metrika/* endpoints can translate them into a 503
    "service not configured" response without crashing.
    """
    return YandexMetrikaClient(settings=settings)


def _metrika_error_to_503(exc: YandexMetrikaError) -> HTTPException:
    """Translate a missing-config error into a 503 (service not configured)."""
    return HTTPException(
        status_code=503,
        detail={
            "error_type": "YandexMetrikaError",
            "message": str(exc),
        },
    )


def _metrika_error_to_502(exc: YandexMetrikaError) -> HTTPException:
    """Translate an upstream / transport error into a 502 with no token echo."""
    return HTTPException(
        status_code=502,
        detail={
            "error_type": "YandexMetrikaError",
            "message": str(exc),
        },
    )


def _raise_metrika_http_error(exc: YandexMetrikaError) -> None:
    if isinstance(exc, YandexMetrikaMissingTokenError):
        raise _metrika_error_to_503(exc) from exc
    raise _metrika_error_to_502(exc) from exc


@app.get(
    "/metrika/counters",
    response_model=YandexMetrikaResult,
    responses=METRIKA_ERROR_RESPONSES,
)
def metrika_counters(
    client: YandexMetrikaClient = Depends(get_yandex_metrika_client),
) -> YandexMetrikaResult:
    """List Metrika counters accessible by the configured OAUTH token."""
    try:
        result = client.list_counters()
    except YandexMetrikaError as exc:
        _raise_metrika_http_error(exc)
    return YandexMetrikaResult(
        service="management",
        method="counters",
        data=result["data"],
    )


@app.get(
    "/metrika/counters/{counter_id}/goals",
    response_model=YandexMetrikaResult,
    responses=METRIKA_ERROR_RESPONSES,
)
def metrika_counter_goals(
    counter_id: int,
    client: YandexMetrikaClient = Depends(get_yandex_metrika_client),
) -> YandexMetrikaResult:
    """List goals for one Metrika counter."""
    try:
        result = client.goals(counter_id)
    except YandexMetrikaError as exc:
        _raise_metrika_http_error(exc)
    return YandexMetrikaResult(
        service="management",
        method="counter_goals",
        counter_id=counter_id,
        data=result["data"],
    )


@app.get(
    "/metrika/counters/{counter_id}/summary",
    response_model=YandexMetrikaResult,
    responses=METRIKA_ERROR_RESPONSES,
)
def metrika_counter_summary(
    counter_id: int,
    date1: str,
    date2: str,
    client: YandexMetrikaClient = Depends(get_yandex_metrika_client),
) -> YandexMetrikaResult:
    """Goals-conversion summary (any-goal reaches per day) for date1..date2.

    Uses the documented ``ym:s:anyGoalReaches`` metric, NOT the per-goal
    ``ym:s:goalReaches`` (the latter is per-goal and is no longer a valid
    metric name in v2).
    """
    try:
        result = client.summary(counter_id, date1=date1, date2=date2)
    except YandexMetrikaError as exc:
        _raise_metrika_http_error(exc)
    return YandexMetrikaResult(
        service="stat",
        method="summary",
        counter_id=counter_id,
        data=result["data"],
    )


@app.get(
    "/metrika/counters/{counter_id}/traffic-sources",
    response_model=YandexMetrikaResult,
    responses=METRIKA_ERROR_RESPONSES,
)
def metrika_counter_traffic_sources(
    counter_id: int,
    date1: str,
    date2: str,
    limit: int = 10,
    client: YandexMetrikaClient = Depends(get_yandex_metrika_client),
) -> YandexMetrikaResult:
    """Visits split by the last-sign traffic source.

    Uses the documented ``ym:s:lastsignTrafficSource`` dimension, NOT the
    older ``ym:s:TrafficSource`` (which is deprecated and breaks in v2).
    """
    try:
        result = client.traffic_sources(
            counter_id, date1=date1, date2=date2, limit=limit
        )
    except YandexMetrikaError as exc:
        _raise_metrika_http_error(exc)
    return YandexMetrikaResult(
        service="stat",
        method="traffic_sources",
        counter_id=counter_id,
        data=result["data"],
    )
