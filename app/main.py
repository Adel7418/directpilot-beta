from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services import check_yandex_direct
from app.models import (
    ApplyActionRequest,
    ApplyActionResult,
    ApprovalResult,
    AuditCheck,
    AuditEvent,
    AuditLog,
    BudgetSimulationRequest,
    BudgetSimulationResult,
    Campaign,
    CampaignAuditResult,
    CampaignDraft,
    CampaignDraftRequest,
    CampaignList,
    Recommendation,
    RecommendationList,
    ReportSummary,
    UtmGenerateRequest,
    UtmGenerateResult,
)

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
            ("/demo/campaigns", "Mock campaigns"),
            ("/demo/report", "Mock report"),
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
    <span class="badge">MVP demo</span>
    <h1>DirectPilot Beta</h1>
    <p>Демонстрационный интерфейс без записи в Яндекс: только mock-данные и read-only сценарии для заявки на Yandex Direct API.</p>
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
          <h2>DirectPilot Beta — демо-стенд для заявки на Yandex Direct API</h2>
          <p>Показывает ценность продукта до одобрения доступа: аудит кампаний, отчёты, рекомендации и контроль согласования.</p>
          <ul>
            <li>Интеграция с Direct API пока представлена безопасным статусом.</li>
            <li>Все кампании, отчёты и рекомендации построены на mock-данных.</li>
            <li>Любое применение изменений требует явного approve и поддерживает dry-run.</li>
          </ul>
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
          <p class="safe">Direct API пока не используется в live-режиме.</p>
          <p>Текущий режим приложения: <strong>{escape(settings.directpilot_mode)}</strong>.</p>
          <p>Демо не показывает секреты, не читает локальные конфигурационные файлы и не выполняет live-записи.</p>
        </section>
        """,
    )


@app.get("/demo/campaigns", response_class=HTMLResponse, include_in_schema=False)
def demo_campaigns() -> HTMLResponse:
    campaigns = list_campaigns().items
    rows = "".join(
        f"<tr><td>{escape(c.name)}</td><td>{escape(c.business_type)}</td><td>{escape(c.status)}</td><td>{c.spend:.0f} ₽</td><td>{c.clicks}</td></tr>"
        for c in campaigns
    )
    return demo_layout(
        "Mock campaigns",
        f"""
        <section class="card">
          <h2>Mock campaigns</h2>
          <table><thead><tr><th>Кампания</th><th>Тип бизнеса</th><th>Статус</th><th>Расход</th><th>Клики</th></tr></thead><tbody>{rows}</tbody></table>
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
        f"<li><strong>{escape(r.id)}</strong>: {escape(r.reason)} Риск: {escape(r.risk_level)}. <button disabled>approve</button> <button disabled>reject</button></li>"
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
