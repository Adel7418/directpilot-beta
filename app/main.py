from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.config import Settings, get_settings
from app.services import check_yandex_direct
from app.models import (
    AdCreate,
    AdGroupCreate,
    AdGroupUpdate,
    AdUpdate,
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
    NegativeKeywordsReplace,
    PreviewPayload,
    RecommendationList,
    ReportSummary,
    UtmGenerateRequest,
    UtmGenerateResult,
    ValidationResult,
    YandexAd,
    YandexAdGroup,
    YandexAdGroupList,
    YandexAdList,
    YandexCampaign,
    YandexCampaignList,
    YandexControlRequest,
    YandexControlResult,
    YandexKeyword,
    YandexKeywordList,
    YandexRawResult,
    YandexSearchQueriesReport,
    YandexSearchQuery,
)
from app.store import store
from app.yandex_direct import YandexDirectClient, YandexDirectError
from app.yandex_facade import mock_yandex


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
    version="0.1.0",
    description="Standalone API-first beta app for safe Yandex Direct automation.",
)


def demo_layout(title: str, content: str) -> HTMLResponse:
    nav = "".join(
        f'<a href="{href}">{label}</a>'
        for href, label in [
            ("/", "Главная"),
            ("/demo/yandex-status", "Статус Яндекса"),
            ("/demo/campaigns", "Yandex campaigns"),
            ("/demo/report", "Report"),
            ("/demo/recommendations", "Recommendations"),
            ("/demo/tools", "Tools"),
            ("/demo/security-approval", "Security/approval flow"),
        ]
    )
    html = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} · DirectPilot Beta</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; color: #172033; background: #f7f8fb; }}
    header, main {{ max-width: 960px; margin: 0 auto; padding: 24px; }}
    header {{ background: #fff; border-bottom: 1px solid #dde3ee; }}
    nav {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 16px; }}
    nav a {{ color: #0b57d0; text-decoration: none; font-weight: 600; }}
    .card {{ background: #fff; border: 1px solid #dde3ee; border-radius: 14px; padding: 20px; margin: 16px 0; }}
    .badge {{ display: inline-block; background: #e8f3ff; color: #064a9b; border-radius: 999px; padding: 4px 10px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; border-bottom: 1px solid #dde3ee; padding: 8px; }}
    .safe {{ color: #146c2e; font-weight: 700; }}
  </style>
</head>
<body>
  <header>
    <span class="badge">Live read-only</span>
    <h1>DirectPilot Beta</h1>
    <p>Интерфейс DirectPilot Beta для чтения реальных данных Яндекс Директа без live-записей.</p>
    <nav>{nav}</nav>
  </header>
  <main>{content}</main>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def demo_home() -> HTMLResponse:
    return demo_layout(
        "Главная",
        """
        <section class="card">
          <h2>DirectPilot Beta — API-first слой для Яндекс Директа</h2>
          <p>Показывает real-data read-only сценарий: доступ к кампаниям, группам, объявлениям и ключевым фразам через внешний REST API.</p>
          <ul>
            <li>Direct API используется в режиме <code>live_readonly</code> для production-данных.</li>
            <li>Live write-вызовы заблокированы до отдельного режима <code>live_write</code>.</li>
            <li>Любое применение изменений требует явного approve, idempotency key и поддерживает dry-run.</li>
          </ul>
          <h3>API demo endpoints</h3>
          <ul>
            <li><a href="/docs">OpenAPI docs</a></li>
            <li><a href="/campaigns">GET /campaigns</a></li>
            <li><a href="/campaign-drafts">GET /campaign-drafts</a></li>
            <li><a href="/audit/campaigns">GET /audit/campaigns</a></li>
            <li><a href="/recommendations">GET /recommendations</a></li>
            <li><a href="/audit-log">GET /audit-log</a></li>
            <li><a href="/integrations/yandex/direct/status">GET /integrations/yandex/direct/status</a></li>
          </ul>
          <p class="safe">Real data, no live writes: DirectPilot Beta читает production-данные и не меняет настройки Директа в live_readonly.</p>
        </section>
        """,
    )


@app.get("/demo/yandex-status", response_class=HTMLResponse, include_in_schema=False)
def demo_yandex_status() -> HTMLResponse:
    settings = get_settings()
    return demo_layout(
        "Статус Яндекса",
        f"""
        <section class="card">
          <h2>Статус доступа к Yandex Direct API</h2>
          <p class="safe">Direct API используется для real-data read-only доступа.</p>
          <p>Текущий режим приложения: <strong>{escape(settings.directpilot_mode)}</strong>.</p>
          <p>Интерфейс не показывает секреты и не выполняет live-записи.</p>
        </section>
        """,
    )


@app.get("/demo/campaigns", response_class=HTMLResponse, include_in_schema=False)
def demo_campaigns() -> HTMLResponse:
    settings = get_settings()
    client = get_yandex_client(settings)
    campaigns = yandex_campaigns(settings=settings, client=client).items
    rows = "".join(
        f"<tr><td>{escape(c.name)}</td><td>{escape(c.type)}</td><td>{escape(c.status)}</td><td>{c.daily_budget:.0f} ₽</td></tr>"
        for c in campaigns
    )
    return demo_layout(
        "Yandex campaigns",
        f"""
        <section class="card">
          <h2>Yandex campaigns</h2>
          <p>Источник: production API Директа, режим read-only.</p>
          <table><thead><tr><th>Кампания</th><th>Тип</th><th>Статус</th><th>Дневной бюджет</th></tr></thead><tbody>{rows}</tbody></table>
        </section>
        """,
    )


@app.get("/demo/report", response_class=HTMLResponse, include_in_schema=False)
def demo_report() -> HTMLResponse:
    report = report_summary()
    return demo_layout(
        "Mock report",
        f"""
        <section class="card">
          <h2>Сводный mock-отчёт</h2>
          <p>Показывает read-only аналитику до подключения Direct API.</p>
          <ul><li>Показы: {report.impressions}</li><li>Клики: {report.clicks}</li><li>CTR: {report.ctr}%</li><li>CPC: {report.cpc} ₽</li><li>Расход: {report.spend} ₽</li></ul>
        </section>
        """,
    )


@app.get("/demo/recommendations", response_class=HTMLResponse, include_in_schema=False)
def demo_recommendations() -> HTMLResponse:
    recs = list_recommendations().items
    items = "".join(
        f'<li><strong>{escape(r.id)}</strong>: {escape(r.reason)} Риск: {escape(r.risk_level)}. <button disabled>approve</button> <button disabled>reject</button></li>'
        for r in recs
    )
    return demo_layout(
        "Recommendations",
        f"""
        <section class="card">
          <h2>Recommendations</h2>
          <p>Все рекомендации требуют явного approve/reject; кнопки на странице демонстрационные.</p>
          <ul>{items}</ul>
        </section>
        """,
    )


@app.get("/demo/tools", response_class=HTMLResponse, include_in_schema=False)
def demo_tools() -> HTMLResponse:
    return demo_layout(
        "Tools",
        """
        <section class="card">
          <h2>Инструменты из eLama-референса для MVP</h2>
          <ul>
            <li><strong>Campaign Audit</strong>: проверка UTM, целей Метрики и дорогих кликов.</li>
            <li><strong>UTM Generator</strong>: быстрая разметка ссылок под Яндекс CPC.</li>
            <li><strong>Budget Simulator</strong>: dry-run оценка кликов и конверсий без изменения ставок.</li>
          </ul>
          <p class="safe">Все инструменты работают как demo/sandbox и не выполняют live-записи.</p>
        </section>
        """,
    )


@app.get("/demo/security-approval", response_class=HTMLResponse, include_in_schema=False)
def demo_security_approval() -> HTMLResponse:
    return demo_layout(
        "Security/approval flow",
        """
        <section class="card">
          <h2>Security/approval flow</h2>
          <ol>
            <li>Сначала mock/read-only анализ.</li>
            <li>Затем рекомендация с уровнем риска и audit_id.</li>
            <li>Применение возможно только после explicit approval и с idempotency key.</li>
            <li class="safe">live-записи отключены до отдельного одобрения и настройки режима.</li>
          </ol>
        </section>
        """,
    )


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
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "sitelinks", "get", lambda c: c.sitelinks_get())


@app.get("/yandex/vcards", response_model=YandexRawResult)
def yandex_vcards(
    settings: Settings = Depends(get_settings),
    client: YandexDirectClient | None = Depends(get_yandex_client),
) -> YandexRawResult:
    return _call_raw_read(settings, client, "vcards", "get", lambda c: c.vcards_get())


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


@app.get("/yandex/keywords-research/wordstat/create", response_model=YandexRawResult)
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


@app.get("/yandex/keywords-research/wordstat/{report_id}", response_model=YandexRawResult)
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


@app.delete("/yandex/keywords-research/wordstat/{report_id}", response_model=YandexRawResult)
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

@app.get("/yandex/reports/summary", response_model=ReportSummary)
def yandex_reports_summary() -> ReportSummary:
    data = mock_yandex.report_summary()
    return ReportSummary(**data, source="mock")


@app.get(
    "/yandex/reports/search-queries",
    response_model=YandexSearchQueriesReport,
)
def yandex_search_queries() -> YandexSearchQueriesReport:
    items = [YandexSearchQuery(**q) for q in mock_yandex.search_queries()]
    return YandexSearchQueriesReport(
        period="last_7_days",
        items=items,
        source="mock",
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
