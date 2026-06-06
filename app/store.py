from __future__ import annotations

from itertools import count
from typing import Iterable, Literal

from app.models import (
    Ad,
    AdCreate,
    AdGroup,
    AdGroupCreate,
    AdGroupUpdate,
    AdUpdate,
    ApplyActionResult,
    AuditEvent,
    BidUpdate,
    BudgetSettings,
    BudgetUpdate,
    Campaign,
    CampaignDraft,
    CampaignDraftRequest,
    GenerateStructureRequest,
    NegativeKeywordsReplace,
    PreviewPayload,
    Recommendation,
    ValidationIssue,
    ValidationResult,
    YandexControlRequest,
    YandexControlResult,
)


def _normalize_phrase(value: str) -> str:
    return " ".join(value.split()).strip().lower()


class MockStore:
    def __init__(self) -> None:
        self._draft_counter = count(1)
        self._ad_group_counter = count(1)
        self._ad_counter = count(1)
        self._audit_counter = count(1)
        self._yandex_action_counter = count(1)

        self.campaigns: dict[str, Campaign] = {
            "cmp_mock_local_services": Campaign(
                id="cmp_mock_local_services",
                name="Mock: локальные услуги",
                business_type="local_services",
                status="active",
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
        self.yandex_actions_by_key: dict[str, YandexControlResult] = {}
        # In-memory Yandex campaign status mirror (mock only).
        self.yandex_campaign_status: dict[str, str] = {
            "cmp_mock_local_services": "active",
        }

    # ------------------------------------------------------------------ audit

    def append_audit(
        self,
        action: str,
        entity: str,
        *,
        actor: str = "agent",
        dry_run: bool = True,
        details: dict | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=f"audit_mock_{next(self._audit_counter) + 1:03d}",
            actor=actor,
            action=action,
            entity=entity,
            dry_run=dry_run,
            details=details,
        )
        self.audit_events.append(event)
        return event

    # ------------------------------------------------------------------ lookups

    def get_draft(self, draft_id: str) -> CampaignDraft:
        return self.drafts[draft_id]

    # ----------------------------------------------------------------- drafts

    def create_draft(self, payload: CampaignDraftRequest) -> CampaignDraft:
        draft_id = f"draft_{next(self._draft_counter):03d}"
        draft = CampaignDraft(
            id=draft_id,
            name=payload.name,
            business_type=payload.business_type,
            region=payload.region,
            monthly_budget=payload.monthly_budget,
            landing_url=payload.landing_url,
            keywords=[
                f"{payload.business_type} {payload.region}",
                f"заказать {payload.business_type}",
            ],
            budget=BudgetSettings(
                daily_budget=None,
                monthly_budget=payload.monthly_budget,
                strategy="manual",
            ),
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

    def update_draft_base(self, draft_id: str, updates: dict) -> CampaignDraft:
        draft = self.drafts[draft_id]
        data = draft.model_dump()
        for key, value in updates.items():
            if value is not None:
                data[key] = value
        data["budget"] = {
            **draft.budget.model_dump(),
            "monthly_budget": data["monthly_budget"],
        }
        updated = CampaignDraft.model_validate(data)
        self.drafts[draft_id] = updated
        self.append_audit(
            "campaign_draft_base_updated",
            draft_id,
            details={"fields": sorted(k for k, v in updates.items() if v is not None)},
        )
        return updated

    # ---------------------------------------------------------------- keywords

    def append_keywords(self, draft_id: str, keywords: Iterable[str]) -> CampaignDraft:
        draft = self.drafts[draft_id]
        new_phrases = [k for k in keywords]
        seen = {_normalize_phrase(p) for p in draft.keywords}
        merged: list[str] = list(draft.keywords)
        for phrase in new_phrases:
            key = _normalize_phrase(phrase)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(phrase.strip())
        draft.keywords = merged
        self.append_audit("campaign_draft_keywords_appended", draft_id, details={"count": len(new_phrases)})
        return draft

    def remove_keywords(self, draft_id: str, keywords: Iterable[str]) -> CampaignDraft:
        draft = self.drafts[draft_id]
        to_remove = {_normalize_phrase(p) for p in keywords}
        before = len(draft.keywords)
        draft.keywords = [p for p in draft.keywords if _normalize_phrase(p) not in to_remove]
        removed = before - len(draft.keywords)
        self.append_audit("campaign_draft_keywords_removed", draft_id, details={"removed": removed})
        return draft

    def replace_keywords(self, draft_id: str, keywords: Iterable[str]) -> CampaignDraft:
        draft = self.drafts[draft_id]
        new_keywords = list(keywords)
        draft.keywords = new_keywords
        self.append_audit("campaign_draft_keywords_updated", draft_id, details={"count": len(new_keywords)})
        return draft

    def replace_negative_keywords(
        self, draft_id: str, payload: NegativeKeywordsReplace
    ) -> CampaignDraft:
        draft = self.drafts[draft_id]
        # de-dup but preserve order
        seen: set[str] = set()
        unique: list[str] = []
        for phrase in payload.negative_keywords:
            key = _normalize_phrase(phrase)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(phrase.strip())
        draft.negative_keywords = unique
        self.append_audit(
            "campaign_draft_negative_keywords_replaced",
            draft_id,
            details={"count": len(unique)},
        )
        return draft

    # ----------------------------------------------------------------- groups

    def _new_ad_group_id(self) -> str:
        return f"adg_{next(self._ad_group_counter):04d}"

    def _new_ad_id(self) -> str:
        return f"ad_{next(self._ad_counter):04d}"

    def create_ad_group(self, draft_id: str, payload: AdGroupCreate) -> CampaignDraft:
        draft = self.drafts[draft_id]
        group = AdGroup(
            id=self._new_ad_group_id(),
            name=payload.name,
            keywords=list(payload.keywords),
        )
        draft.ad_groups.append(group)
        self.append_audit("campaign_draft_ad_group_created", draft_id, details={"group_id": group.id})
        return draft

    def update_ad_group(
        self, draft_id: str, group_id: str, payload: AdGroupUpdate
    ) -> CampaignDraft:
        draft = self.drafts[draft_id]
        for group in draft.ad_groups:
            if group.id == group_id:
                if payload.name is not None:
                    group.name = payload.name
                if payload.keywords is not None:
                    group.keywords = list(payload.keywords)
                self.append_audit(
                    "campaign_draft_ad_group_updated",
                    draft_id,
                    details={"group_id": group_id},
                )
                return draft
        raise KeyError(group_id)

    def delete_ad_group(self, draft_id: str, group_id: str) -> CampaignDraft:
        draft = self.drafts[draft_id]
        draft.ad_groups = [g for g in draft.ad_groups if g.id != group_id]
        # remove orphan ads
        orphan_ads = [a.id for a in draft.ads if a.ad_group_id == group_id]
        draft.ads = [a for a in draft.ads if a.ad_group_id != group_id]
        self.append_audit(
            "campaign_draft_ad_group_deleted",
            draft_id,
            details={"group_id": group_id, "orphan_ads": orphan_ads},
        )
        return draft

    # ------------------------------------------------------------------- ads

    def create_ad(self, draft_id: str, payload: AdCreate) -> CampaignDraft:
        draft = self.drafts[draft_id]
        if not any(g.id == payload.ad_group_id for g in draft.ad_groups):
            raise KeyError(payload.ad_group_id)
        ad = Ad(
            id=self._new_ad_id(),
            ad_group_id=payload.ad_group_id,
            title=payload.title,
            text=payload.text,
            landing_url=payload.landing_url,
            display_link_path=payload.display_link_path,
        )
        draft.ads.append(ad)
        self.append_audit("campaign_draft_ad_created", draft_id, details={"ad_id": ad.id})
        return draft

    def update_ad(self, draft_id: str, ad_id: str, payload: AdUpdate) -> CampaignDraft:
        draft = self.drafts[draft_id]
        for ad in draft.ads:
            if ad.id == ad_id:
                if payload.title is not None:
                    ad.title = payload.title
                if payload.text is not None:
                    ad.text = payload.text
                if payload.landing_url is not None:
                    ad.landing_url = payload.landing_url
                if payload.display_link_path is not None:
                    ad.display_link_path = payload.display_link_path
                self.append_audit(
                    "campaign_draft_ad_updated", draft_id, details={"ad_id": ad_id}
                )
                return draft
        raise KeyError(ad_id)

    def delete_ad(self, draft_id: str, ad_id: str) -> CampaignDraft:
        draft = self.drafts[draft_id]
        before = len(draft.ads)
        draft.ads = [a for a in draft.ads if a.id != ad_id]
        removed = before - len(draft.ads)
        self.append_audit(
            "campaign_draft_ad_deleted", draft_id, details={"removed": removed}
        )
        return draft

    # ------------------------------------------------------------- structure

    def generate_structure(
        self, draft_id: str, payload: GenerateStructureRequest
    ) -> CampaignDraft:
        draft = self.drafts[draft_id]
        topic = payload.topic.strip()
        region = payload.region.strip()
        # Use the requested region for generated phrases; do not mutate draft.region here.
        groups: list[AdGroup] = []
        ad_drafts: list[Ad] = []
        existing_keyword_keys = {_normalize_phrase(p) for p in draft.keywords}
        for i in range(payload.group_count):
            group_name = f"{topic.title()} — группа {i + 1}"
            phrases: list[str] = []
            for k in range(payload.keywords_per_group):
                phrase = f"{topic} {region}".strip()
                if k > 0:
                    phrase = f"{topic} {region} вариант {k + 1}"
                if i > 0:
                    phrase = f"{phrase} сегмент {i + 1}"
                key = _normalize_phrase(phrase)
                if key in existing_keyword_keys:
                    continue
                existing_keyword_keys.add(key)
                phrases.append(phrase)
            group = AdGroup(
                id=self._new_ad_group_id(),
                name=group_name,
                keywords=phrases,
            )
            groups.append(group)
            # add a draft ad per group
            ad = Ad(
                id=self._new_ad_id(),
                ad_group_id=group.id,
                title=f"{group_name} — заголовок",
                text=f"Mock-объявление для группы «{group_name}».",
                landing_url=draft.landing_url,
            )
            ad_drafts.append(ad)
        # append to draft
        draft.ad_groups.extend(groups)
        draft.ads.extend(ad_drafts)
        # extend keywords
        seen = {_normalize_phrase(p) for p in draft.keywords}
        for g in groups:
            for p in g.keywords:
                key = _normalize_phrase(p)
                if key in seen:
                    continue
                seen.add(key)
                draft.keywords.append(p)
        # add baseline negative keywords
        baseline_negatives = ["своими руками", "бесплатно", "скачать", "diy"]
        for neg in baseline_negatives:
            if neg not in draft.negative_keywords:
                draft.negative_keywords.append(neg)
        self.append_audit(
            "campaign_draft_structure_generated",
            draft_id,
            details={
                "groups": len(groups),
                "ads": len(ad_drafts),
                "keywords": sum(len(g.keywords) for g in groups),
            },
        )
        return draft

    # --------------------------------------------------------------- validate

    def validate_draft(self, draft_id: str) -> ValidationResult:
        draft = self.drafts[draft_id]
        issues: list[ValidationIssue] = []
        # ad groups
        if not draft.ad_groups:
            issues.append(
                ValidationIssue(
                    code="missing_ad_groups",
                    severity="error",
                    message="В черновике нет групп объявлений.",
                )
            )
        # ads
        if not draft.ads:
            issues.append(
                ValidationIssue(
                    code="missing_ads",
                    severity="error",
                    message="В черновике нет объявлений.",
                )
            )
        # keywords (group-level or draft-level)
        group_keywords = [p for g in draft.ad_groups for p in g.keywords]
        if not group_keywords and not draft.keywords:
            issues.append(
                ValidationIssue(
                    code="missing_keywords",
                    severity="error",
                    message="Не задано ни одной ключевой фразы.",
                )
            )
        # landing url
        if not draft.landing_url or not draft.landing_url.startswith(("http://", "https://")):
            issues.append(
                ValidationIssue(
                    code="invalid_landing_url",
                    severity="error",
                    message="Landing URL должен начинаться с http(s)://",
                )
            )
        # budget
        if draft.budget.daily_budget is None and draft.budget.monthly_budget is None:
            issues.append(
                ValidationIssue(
                    code="missing_budget",
                    severity="error",
                    message="Не задан ни дневной, ни месячный бюджет.",
                )
            )
        # region
        if not draft.region or len(draft.region) < 2:
            issues.append(
                ValidationIssue(
                    code="missing_region",
                    severity="error",
                    message="Не указан регион показа.",
                )
            )
        # warnings
        if not draft.negative_keywords:
            issues.append(
                ValidationIssue(
                    code="no_negative_keywords",
                    severity="warning",
                    message="Минус-слова не заданы. Рекомендуется добавить хотя бы несколько.",
                )
            )
        # duplicates. Generated group keywords are also mirrored into draft.keywords,
        # so do not concatenate both lists or every generated phrase becomes a false duplicate.
        all_keywords = list(draft.keywords)
        seen: set[str] = set()
        duplicates: list[str] = []
        for phrase in all_keywords:
            key = _normalize_phrase(phrase)
            if key in seen and key not in duplicates:
                duplicates.append(key)
            seen.add(key)
        if duplicates:
            issues.append(
                ValidationIssue(
                    code="duplicate_keywords",
                    severity="warning" if len(duplicates) < 3 else "error",
                    message=f"Найдены повторяющиеся ключевые фразы: {', '.join(duplicates[:5])}",
                )
            )
        valid = not any(i.severity == "error" for i in issues)
        return ValidationResult(valid=valid, issues=issues)

    # ----------------------------------------------------------------- preview

    def preview_draft(self, draft_id: str) -> PreviewPayload:
        draft = self.drafts[draft_id]
        yandex_payload = {
            "method": "create",
            "campaign": {
                "name": draft.name or f"{draft.business_type} {draft.region}",
                "business_type": draft.business_type,
                "region": draft.region,
                "landing_url": draft.landing_url,
                "budget": draft.budget.model_dump(exclude_none=True),
                "bids": draft.bids.model_dump(exclude_none=True),
            },
            "ad_groups": [g.model_dump() for g in draft.ad_groups],
            "ads": [a.model_dump() for a in draft.ads],
            "negative_keywords": list(draft.negative_keywords),
            "params": {
                "Campaign": {
                    "Name": draft.name or f"{draft.business_type} {draft.region}",
                    "BusinessType": draft.business_type,
                    "Region": draft.region,
                    "LandingUrl": draft.landing_url,
                    "Budget": draft.budget.model_dump(exclude_none=True),
                    "Bids": draft.bids.model_dump(exclude_none=True),
                },
                "AdGroups": [
                    {
                        "Name": g.name,
                        "Keywords": g.keywords,
                        "NegativeKeywords": draft.negative_keywords,
                    }
                    for g in draft.ad_groups
                ],
                "Ads": [
                    {
                        "AdGroupId": a.ad_group_id,
                        "Title": a.title,
                        "Text": a.text,
                        "LandingUrl": a.landing_url,
                        "DisplayLinkPath": a.display_link_path,
                    }
                    for a in draft.ads
                ],
            },
        }
        return PreviewPayload(
            draft_id=draft_id,
            name=draft.name or f"{draft.business_type} {draft.region}",
            business_type=draft.business_type,
            region=draft.region,
            landing_url=draft.landing_url,
            budget=draft.budget,
            bids=draft.bids,
            ad_groups=draft.ad_groups,
            ads=draft.ads,
            negative_keywords=draft.negative_keywords,
            yandex_payload=yandex_payload,
            dry_run=True,
            requires_approval=True,
        )

    # ----------------------------------------------------------------- budget

    def update_budget(self, draft_id: str, payload: BudgetUpdate) -> CampaignDraft:
        draft = self.drafts[draft_id]
        if payload.daily_budget is not None:
            draft.budget.daily_budget = payload.daily_budget
        if payload.monthly_budget is not None:
            draft.budget.monthly_budget = payload.monthly_budget
            draft.monthly_budget = payload.monthly_budget
        if payload.strategy is not None:
            draft.budget.strategy = payload.strategy
        self.append_audit(
            "campaign_draft_budget_updated",
            draft_id,
            details=payload.model_dump(exclude_none=True),
        )
        return draft

    # ------------------------------------------------------------------- bids

    def update_bids(self, draft_id: str, payload: BidUpdate) -> CampaignDraft:
        draft = self.drafts[draft_id]
        if payload.max_cpc is not None:
            draft.bids.max_cpc = payload.max_cpc
        if payload.keyword_bids is not None:
            # merge: explicit bids win
            merged = dict(draft.bids.keyword_bids)
            merged.update(payload.keyword_bids)
            draft.bids.keyword_bids = merged
        self.append_audit(
            "campaign_draft_bids_updated",
            draft_id,
            details={
                "max_cpc": payload.max_cpc,
                "keyword_bids": payload.keyword_bids,
            },
        )
        return draft

    # ----------------------------------------------------- yandex control

    def yandex_control(
        self,
        campaign_id: str,
        action: Literal["pause", "resume"],
        payload: YandexControlRequest,
    ) -> YandexControlResult:
        if not payload.approved:
            raise ValueError("Action requires explicit approval")
        cache_key = f"{action}:{campaign_id}:{payload.idempotency_key}"
        if cache_key in self.yandex_actions_by_key:
            return self.yandex_actions_by_key[cache_key]
        new_status = "paused" if action == "pause" else "active"
        audit = self.append_audit(
            f"yandex_{action}_requested",
            campaign_id,
            dry_run=payload.dry_run,
            details={
                "approved": payload.approved,
                "idempotency_key": payload.idempotency_key,
                "reason": payload.reason,
                "new_status": new_status,
            },
        )
        result = YandexControlResult(
            campaign_id=campaign_id,
            action=action,
            dry_run=payload.dry_run,
            applied=not payload.dry_run,
            source="mock",
            audit_id=audit.id,
            new_status=new_status,
        )
        # mutate mock state only if not dry run
        if not payload.dry_run:
            self.yandex_campaign_status[campaign_id] = new_status
        self.yandex_actions_by_key[cache_key] = result
        return result


store = MockStore()
