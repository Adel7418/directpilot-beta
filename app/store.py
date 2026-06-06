from itertools import count

from app.models import (
    ApplyActionResult,
    AuditEvent,
    Campaign,
    CampaignDraft,
    CampaignDraftRequest,
    Recommendation,
)


class MockStore:
    def __init__(self) -> None:
        self._draft_counter = count(1)
        self._audit_counter = count(1)
        self.campaigns: dict[str, Campaign] = {
            "cmp_mock_local_services": Campaign(
                id="cmp_mock_local_services",
                name="Mock: локальные услуги",
                business_type="local_services",
                status="draft_readonly",
                spend=1250.0,
                clicks=42,
            )
        }
        self.drafts: dict[str, CampaignDraft] = {}
        self.recommendations: dict[str, Recommendation] = {
            "rec_mock_pause_keyword": Recommendation(
                id="rec_mock_pause_keyword",
                action_id="act_mock_pause_keyword",
                reason="Ключ потратил бюджет в mock-отчёте и не имеет конверсий.",
                risk_level="low",
                status="pending",
            ),
            "rec_mock_add_utm": Recommendation(
                id="rec_mock_add_utm",
                action_id="act_mock_add_utm",
                reason="Часть объявлений ведёт на посадочные страницы без UTM-разметки.",
                risk_level="medium",
                status="pending",
            ),
            "rec_mock_budget_limit": Recommendation(
                id="rec_mock_budget_limit",
                action_id="act_mock_budget_limit",
                reason="Дневной бюджет ограничивает показы в mock-прогнозе.",
                risk_level="low",
                status="pending",
            ),
        }
        self.audit_events: list[AuditEvent] = [
            AuditEvent(
                id="audit_mock_001",
                actor="system",
                action="mock_healthcheck",
                entity="directpilot-beta",
            )
        ]
        self.apply_results_by_key: dict[str, ApplyActionResult] = {}

    def append_audit(self, action: str, entity: str, *, actor: str = "agent", dry_run: bool = True) -> AuditEvent:
        event = AuditEvent(
            id=f"audit_mock_{next(self._audit_counter) + 1:03d}",
            actor=actor,
            action=action,
            entity=entity,
            dry_run=dry_run,
        )
        self.audit_events.append(event)
        return event

    def create_draft(self, payload: CampaignDraftRequest) -> CampaignDraft:
        draft_id = f"draft_{next(self._draft_counter):03d}"
        draft = CampaignDraft(
            id=draft_id,
            groups=[f"{payload.business_type}: базовая группа"],
            keywords=[f"{payload.business_type} {payload.region}", f"заказать {payload.business_type}"],
        )
        self.drafts[draft_id] = draft
        self.campaigns[draft_id] = Campaign(
            id=draft_id,
            name=f"Draft: {payload.business_type} / {payload.region}",
            business_type=payload.business_type,
            status="draft",
        )
        self.append_audit("campaign_draft_created", draft_id)
        return draft


    def update_draft_keywords(self, draft_id: str, keywords: list[str]) -> CampaignDraft:
        draft = self.drafts[draft_id]
        draft.keywords = list(keywords)
        self.append_audit("campaign_draft_keywords_updated", draft_id)
        return draft


store = MockStore()
