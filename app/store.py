from __future__ import annotations

import datetime as _dt
import uuid as _uuid
from itertools import count
from typing import Any, Iterable, Literal

from app.config import Settings
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
    SemanticChangeApplyRequest,
    SemanticChangeApplyResult,
    SemanticChangeOperation,
    SemanticChangePackage,
    SemanticChangePreview,
    SemanticChangeRequest,
    ValidationIssue,
    ValidationResult,
    YandexControlRequest,
    YandexControlResult,
    YandexVCardRequest,
    YandexVCardResult,
)
from app.yandex_direct import YandexDirectClient, YandexDirectError


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
        # Semantic-change packages (see app.models.SemanticChangePackage).
        # Kept in a dedicated dict so audit / list views can stay simple.
        self.semantic_packages_by_id: dict[str, Any] = {}
        self.semantic_apply_results_by_key: dict[str, Any] = {}
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
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> YandexControlResult:
        if not payload.approved:
            raise ValueError("Action requires explicit approval")
        cache_key = f"{action}:{campaign_id}:{payload.idempotency_key}"
        if cache_key in self.yandex_actions_by_key:
            return self.yandex_actions_by_key[cache_key]
        new_status = "paused" if action == "pause" else "active"

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode in ("sandbox", "live_write")

        # ------------------------------------------------------------------
        # Dry-run paths: never perform a network write.
        # ------------------------------------------------------------------
        if not is_live:
            # Mock mode: pure in-memory mirror.
            audit = self.append_audit(
                f"yandex_{action}_requested",
                campaign_id,
                dry_run=payload.dry_run,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "new_status": new_status,
                    "source": "mock",
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
            if not payload.dry_run:
                self.yandex_campaign_status[campaign_id] = new_status
            self.yandex_actions_by_key[cache_key] = result
            return result

        # ------------------------------------------------------------------
        # Live modes (sandbox / live_readonly).
        # ------------------------------------------------------------------
        if payload.dry_run:
            # No network call; mark source=yandex, applied=False.
            audit = self.append_audit(
                f"yandex_{action}_requested",
                campaign_id,
                dry_run=True,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "new_status": new_status,
                    "source": "yandex",
                    "mode": mode,
                },
            )
            result = YandexControlResult(
                campaign_id=campaign_id,
                action=action,
                dry_run=True,
                applied=False,
                source="yandex",
                audit_id=audit.id,
                new_status=new_status,
            )
            self.yandex_actions_by_key[cache_key] = result
            return result

        if not can_write:
            raise YandexDirectError(
                "Live writes are not allowed in live_readonly mode; use live_write"
            )

        # Real write path: caller must provide a client with a token.
        if client is None:
            raise YandexDirectError(
                "YandexDirectClient is required for live pause/resume writes"
            )

        try:
            yandex_result: dict[str, Any]
            if action == "pause":
                yandex_result = client.suspend_campaign(campaign_id)
            else:
                yandex_result = client.resume_campaign(campaign_id)
            if not yandex_result.get("ok"):
                # Surface as a typed error so endpoints return 502.
                err = yandex_result.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected {action}: error_code="
                    f"{err.get('error_code')!r}"
                )
            # Success: mirror to local mock state for parity with mock mode.
            self.yandex_campaign_status[campaign_id] = new_status
            audit = self.append_audit(
                f"yandex_{action}_requested",
                campaign_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "new_status": new_status,
                    "source": "yandex",
                    "mode": mode,
                    "yandex_result": yandex_result.get("result"),
                    "yandex_units": yandex_result.get("units"),
                },
            )
            result = YandexControlResult(
                campaign_id=campaign_id,
                action=action,
                dry_run=False,
                applied=True,
                source="yandex",
                audit_id=audit.id,
                new_status=new_status,
            )
            self.yandex_actions_by_key[cache_key] = result
            return result
        except YandexDirectError as exc:
            # Record the failure for audit; never include the token.
            self.append_audit(
                f"yandex_{action}_failed",
                campaign_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "source": "yandex",
                    "mode": mode,
                    "yandex_error": str(exc),
                },
            )
            raise


    # --------------------------------------------------------------- yandex vcard

    @staticmethod
    def _vcard_to_direct_params(payload: YandexVCardRequest) -> dict[str, Any]:
        phone: dict[str, Any] = {
            "CountryCode": payload.phone.country_code.lstrip("+"),
            "CityCode": payload.phone.city_code,
            "PhoneNumber": payload.phone.phone_number,
        }
        if payload.phone.extension:
            phone["Extension"] = payload.phone.extension

        vcard: dict[str, Any] = {
            "Country": payload.country,
            "City": payload.city,
            "CompanyName": payload.company_name,
            "WorkTime": payload.work_time,
            "Phone": phone,
        }
        if payload.campaign_id is not None:
            vcard["CampaignId"] = payload.campaign_id
        optional = {
            "ContactPerson": payload.contact_person,
            "Street": payload.street,
            "House": payload.house,
            "Building": payload.building,
            "Apartment": payload.apartment,
            "ExtraMessage": payload.extra_message,
        }
        vcard.update({key: value for key, value in optional.items() if value})
        return vcard

    @staticmethod
    def _extract_vcard_id(response: dict[str, Any]) -> str | None:
        result = response.get("result")
        if not isinstance(result, dict):
            return None
        add_results = result.get("AddResults")
        if isinstance(add_results, list) and add_results:
            first = add_results[0]
            if isinstance(first, dict) and first.get("Id") is not None:
                return str(first["Id"])
        return None

    def yandex_vcard_add(
        self,
        payload: YandexVCardRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> YandexVCardResult:
        if not payload.approved:
            raise ValueError("Action requires explicit approval")

        existing = next(
            (
                event
                for event in self.audit_events
                if event.details
                and event.details.get("idempotency_key") == payload.idempotency_key
                and event.action == "yandex_vcard_add_requested"
            ),
            None,
        )
        if existing is not None:
            details = existing.details or {}
            source = details.get("source", "mock")
            return YandexVCardResult(
                dry_run=existing.dry_run,
                applied=not existing.dry_run,
                source=source if source in ("mock", "yandex") else "mock",
                audit_id=existing.id,
                vcard_id=details.get("vcard_id"),
                work_time=payload.work_time,
            )

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode in ("sandbox", "live_write")
        direct_payload = self._vcard_to_direct_params(payload)

        if payload.dry_run or not is_live:
            audit = self.append_audit(
                "yandex_vcard_add_requested",
                "vcards",
                dry_run=payload.dry_run,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "source": "yandex" if is_live else "mock",
                    "mode": mode,
                    "payload_redacted": direct_payload,
                },
            )
            return YandexVCardResult(
                dry_run=payload.dry_run,
                applied=not payload.dry_run and not is_live,
                source="yandex" if is_live else "mock",
                audit_id=audit.id,
                work_time=payload.work_time,
            )

        if not can_write:
            raise YandexDirectError(
                "Live writes are not allowed in live_readonly mode; use live_write"
            )
        if client is None:
            raise YandexDirectError("YandexDirectClient is required for live vCard writes")

        try:
            yandex_result = client.vcards_add(direct_payload)
            if not yandex_result.get("ok"):
                err = yandex_result.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected vcards.add: error_code={err.get('error_code')!r}"
                )
            vcard_id = self._extract_vcard_id(yandex_result)
            audit = self.append_audit(
                "yandex_vcard_add_requested",
                "vcards",
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "source": "yandex",
                    "mode": mode,
                    "vcard_id": vcard_id,
                    "yandex_units": yandex_result.get("units"),
                },
            )
            return YandexVCardResult(
                dry_run=False,
                applied=True,
                source="yandex",
                audit_id=audit.id,
                vcard_id=vcard_id,
                work_time=payload.work_time,
            )
        except YandexDirectError as exc:
            self.append_audit(
                "yandex_vcard_add_failed",
                "vcards",
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "source": "yandex",
                    "mode": mode,
                    "yandex_error": str(exc),
                },
            )
            raise


    # ------------------------------------------------- semantic change package
    #
    # The semantic-change pipeline is a safe-by-default way to prepare
    # negative-keyword and positive-keyword changes for an existing
    # Yandex Direct campaign (e.g. ``710382063``) and then explicitly
    # apply them. ``prepare`` is pure-local and never touches the
    # network. ``apply`` honours the same gate contract as the rest of
    # the product: ``dry_run`` / ``approved`` / ``idempotency_key``,
    # plus the runtime mode (``live_readonly`` blocks real apply;
    # ``live_write`` allows it).
    #
    # The live-write path uses the v5 services confirmed against the
    # public Direct API docs:
    #
    # * positive keywords -> ``keywords.add``
    #   (service: ``keywords``, method: ``add``)
    # * group-level negative keywords -> ``adgroups.update``
    #   (service: ``adgroups``, method: ``update``) with
    #   ``AdGroups[].NegativeKeywords.Items``
    #
    # Both require an explicit ``ad_group_id`` on the request; the
    # endpoint rejects requests without it with HTTP 400. The store
    # dispatches to ``YandexDirectClient.keywords_add`` and
    # ``YandexDirectClient.adgroups_update`` rather than touching the
    # private ``_call`` itself.

    @staticmethod
    def _normalize_ad_group_id(ad_group_id: int | str | None) -> int | str:
        """Normalise ad_group_id to the form Direct expects in JSON bodies.

        Numeric strings (``"123456"``) become ``int``; everything else is
        passed through unchanged so non-numeric placeholder ids still
        produce a deterministic error from Direct rather than a silent
        type-coercion surprise.
        """
        if isinstance(ad_group_id, str) and ad_group_id.isdigit():
            return int(ad_group_id)
        return ad_group_id  # type: ignore[return-value]

    @staticmethod
    def _semantics_note_for_merge() -> str:
        return (
            "adgroups.update with NegativeKeywords.Items is REPLACE on "
            "Direct API v5; live apply will read the current group-level "
            "negatives via adgroups.get, merge with the requested phrases "
            "(order-preserving, de-duplicated), and write the combined "
            "set. Prepare / dry-run stay pure-local — no network call."
        )

    @staticmethod
    def _build_semantic_operations(
        campaign_id: str,
        payload: SemanticChangeRequest,
    ) -> list[SemanticChangeOperation]:
        """Build the list of v5 operations that would be sent on apply.

        Translation rules (locked against the confirmed v5 docs):

        * ``add_keywords`` -> one ``keywords.add`` operation.
          Service is ``keywords``, method is ``add``, payload is
          ``{"Keywords": [{"Keyword": phrase, "AdGroupId": <id>}, ...]}``.
          Each entry carries the same target ad group (the user-facing
          semantic change is "add these phrases to this group").
        * ``add_negative_keywords`` -> one ``adgroups.update`` operation.
          Service is ``adgroups``, method is ``update``, payload is
          ``{"AdGroups": [{"Id": <id>, "NegativeKeywords":
          {"Items": [<phrase>, ...]}}]}``. Leading ``-`` is stripped
          from every phrase so the v5 service receives the form it
          expects.

        IMPORTANT: ``NegativeKeywords.Items`` is REPLACE on Direct, not
        APPEND. The apply path performs a read-modify-write via
        ``adgroups.get`` + ``adgroups.update`` to preserve pre-existing
        group-level negatives — see ``_apply_with_existing_negatives``.
        The operation built here is the *requested* payload; the
        effective payload on apply is the merged one.
        """
        operations: list[SemanticChangeOperation] = []
        if payload.add_keywords:
            ad_group_id = MockStore._normalize_ad_group_id(payload.ad_group_id)
            operations.append(
                SemanticChangeOperation(
                    method="keywords.add",
                    params={
                        "Keywords": [
                            {"Keyword": phrase, "AdGroupId": ad_group_id}
                            for phrase in payload.add_keywords
                        ],
                    },
                )
            )
        if payload.add_negative_keywords:
            ad_group_id = MockStore._normalize_ad_group_id(payload.ad_group_id)
            items = [
                YandexDirectClient._normalize_negative_phrase(p)
                for p in payload.add_negative_keywords
            ]
            operations.append(
                SemanticChangeOperation(
                    method="adgroups.update",
                    params={
                        "AdGroups": [
                            {
                                "Id": ad_group_id,
                                "NegativeKeywords": {"Items": items},
                            }
                        ],
                    },
                )
            )
        # campaign_id is part of the audit log only — it is NOT threaded
        # into the v5 request body (Direct's keywords.add / adgroups.update
        # resolve the campaign from the AdGroupId).
        _ = campaign_id
        return operations

    def prepare_semantic_change_package(
        self,
        campaign_id: str,
        payload: SemanticChangeRequest,
        *,
        settings: Settings | None = None,
    ) -> SemanticChangePackage:
        """Prepare a semantic change package. Always pure-local.

        No network call. No approval required. The result is a
        ``SemanticChangePackage`` whose ``preview`` lists the v5
        operations that *would* be sent on apply, so the user (or
        another tool) can inspect the proposed change before deciding
        whether to actually apply it.

        Raises ``ValueError`` if the user supplied either keyword list
        without an ``ad_group_id`` — Direct API v5 ``keywords.add``
        requires ``AdGroupId`` per keyword and ``adgroups.update``
        requires the target group ``Id``. Endpoints translate this
        into HTTP 400.
        """
        if not payload.add_negative_keywords and not payload.add_keywords:
            raise ValueError(
                "At least one of add_negative_keywords / add_keywords must be provided"
            )
        if (
            payload.add_negative_keywords or payload.add_keywords
        ) and payload.ad_group_id is None:
            raise ValueError(
                "ad_group_id is required when add_keywords or "
                "add_negative_keywords is provided"
            )
        mode = settings.directpilot_mode if settings is not None else "mock"
        operations = self._build_semantic_operations(campaign_id, payload)
        has_negative_op = bool(payload.add_negative_keywords)
        audit = self.append_audit(
            "semantic_change_prepared",
            campaign_id,
            dry_run=True,
            details={
                "mode": mode,
                "reason": payload.reason,
                "ad_group_id": payload.ad_group_id,
                "add_negative_keywords": payload.add_negative_keywords or [],
                "add_keywords": payload.add_keywords or [],
                "operations_count": len(operations),
                # Mirror the merge marker into audit so reviewers can
                # see from the audit log that live apply will do a
                # read-modify-write for negative keywords. Stays
                # pure-local here — no network call.
                "merge_on_apply": has_negative_op,
                "semantics_note": (
                    MockStore._semantics_note_for_merge() if has_negative_op else None
                ),
            },
        )
        package = SemanticChangePackage(
            package_id=f"scpkg_{_uuid.uuid4().hex[:10]}",
            campaign_id=campaign_id,
            status="prepared",
            mode=mode,
            dry_run=True,
            created_at=_dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
            reason=payload.reason,
            preview=SemanticChangePreview(
                operations=operations,
                merge_on_apply=has_negative_op,
                semantics_note=(
                    MockStore._semantics_note_for_merge()
                    if has_negative_op
                    else None
                ),
            ),
            audit_id=audit.id,
        )
        self.semantic_packages_by_id[package.package_id] = package
        return package

    @staticmethod
    def _normalize_for_merge(phrase: str) -> str:
        """Normalize a phrase for de-duplication on the merge path.

        Mirrors the normalisation applied to the request body
        (strip leading ``-``, collapse whitespace) so the merge
        comparison sees the same string that Direct will eventually
        see.
        """
        return YandexDirectClient._normalize_negative_phrase(phrase)

    @classmethod
    def _merge_negative_phrases(
        cls,
        existing: list[str],
        requested: list[str],
    ) -> list[str]:
        """Merge existing + requested group-level negatives.

        Order-preserving: existing phrases come first (in their current
        order on Direct), then any requested phrase that is not already
        present. De-duplication is done on the normalised form (lowercase,
        whitespace-collapsed, leading ``-`` stripped) to match the
        normalisation the apply path applies to the request body.
        """
        merged: list[str] = []
        seen: set[str] = set()
        for phrase in list(existing) + list(requested):
            key = cls._normalize_for_merge(phrase).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(phrase.strip())
        return merged

    def _apply_with_existing_negatives(
        self,
        *,
        client: YandexDirectClient,
        campaign_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Read-modify-write the negative-keyword list for one ad group.

        Direct API v5 ``adgroups.update`` with ``NegativeKeywords.Items``
        is REPLACE, not APPEND. A naive send would silently drop any
        pre-existing group-level negatives, which is a data-loss risk.
        This helper:

        1. Reads the campaign's ad groups via ``adgroups.get`` (the
           ``NegativeKeywords`` field is requested explicitly so we
           don't have to do a second call per group).
        2. Locates the target group by id. If the response is not ``ok``
           or the group is missing, raises ``YandexDirectError`` so the
           caller can audit ``semantic_change_apply_failed`` BEFORE any
           ``adgroups.update`` is sent.
        3. Merges existing + requested negatives (order-preserving,
           de-duplicated).
        4. Sends ``adgroups.update`` with the merged list.

        The return value is the response envelope from the final
        ``adgroups.update`` call (``{"ok": ..., "result": ..., "units": ...}``).
        """
        ad_groups_param = (params.get("AdGroups") or [{}])[0]
        target_id = ad_groups_param.get("Id")
        if target_id is None:
            raise YandexDirectError(
                "adgroups.update operation is missing the target AdGroup Id"
            )
        requested_items: list[str] = list(
            (ad_groups_param.get("NegativeKeywords") or {}).get("Items") or []
        )

        get_response = client.adgroups_get(campaign_id)
        if not get_response.get("ok"):
            err = get_response.get("error") or {}
            raise YandexDirectError(
                f"Yandex Direct rejected adgroups.get: error_code="
                f"{err.get('error_code')!r}"
            )
        result_payload = get_response.get("result")
        if not isinstance(result_payload, dict):
            raise YandexDirectError(
                "adgroups.get returned an unexpected envelope (no result.AdGroups)"
            )
        raw_ad_groups = result_payload.get("AdGroups")
        if not isinstance(raw_ad_groups, list):
            raise YandexDirectError(
                "adgroups.get returned an unexpected envelope (no result.AdGroups)"
            )

        # Direct returns ids as ints for numeric ids; match the same way
        # we normalise on the request side.
        def _match(group_id: Any) -> bool:
            if isinstance(target_id, int):
                return isinstance(group_id, int) and group_id == target_id
            return str(group_id) == str(target_id)

        existing_items: list[str] = []
        target_found = False
        for group in raw_ad_groups:
            if not isinstance(group, dict) or not _match(group.get("Id")):
                continue
            target_found = True
            neg = group.get("NegativeKeywords")
            if isinstance(neg, dict):
                raw_items = neg.get("Items")
                if isinstance(raw_items, list):
                    existing_items = [
                        str(p) for p in raw_items if isinstance(p, str)
                    ]
            break
        if not target_found:
            # Fail closed BEFORE any update: the target ad group id was
            # not in the adgroups.get response, so we cannot safely
            # merge — the caller will audit semantic_change_apply_failed
            # and re-raise so the endpoint returns 502.
            raise YandexDirectError(
                f"adgroups.get response does not contain the target AdGroup Id={target_id!r}"
            )

        merged_items = self._merge_negative_phrases(existing_items, requested_items)
        merged_payload = {
            "Id": target_id,
            "NegativeKeywords": {"Items": merged_items},
        }
        return client.adgroups_update([merged_payload])

    def apply_semantic_change(
        self,
        package_id: str,
        payload: SemanticChangeApplyRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> SemanticChangeApplyResult:
        """Apply a previously prepared semantic change.

        Gate contract (matches the rest of the product):

        * ``approved`` MUST be ``True`` — otherwise raise ValueError.
        * ``idempotency_key`` MUST be supplied (length >= 6, enforced
          by the Pydantic model) — same key returns the cached result
          without re-sending.
        * ``dry_run=True`` is ALWAYS allowed and NEVER performs a
          network write. The result is an audited preview.
        * In ``live_readonly`` mode, ``dry_run=False`` is REJECTED
          before any network call.
        * In ``live_write`` mode with all gates satisfied, the
          confirmed-shape operations from the package are sent via
          the explicit ``YandexDirectClient`` helpers
          (``keywords_add`` / ``adgroups_update``). Each successful
          operation is counted; on the first failure the whole apply
          aborts and ``semantic_change_apply_failed`` is recorded.
        * ``mock`` and ``sandbox`` modes are NOT product write paths
          for semantic changes (a sandbox flag here would imply the
          operations are safe to test, but the sandbox token has the
          same shape as a real one — we keep the gate strict).
        """
        if not payload.approved:
            raise ValueError("Action requires explicit approval")
        package = self.semantic_packages_by_id.get(package_id)
        if package is None:
            raise KeyError("semantic_change_package_not_found")
        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"

        # Idempotency replay.
        cache_key = f"{package_id}:{payload.idempotency_key}"
        if cache_key in self.semantic_apply_results_by_key:
            return self.semantic_apply_results_by_key[cache_key]

        # Dry-run path: never perform a network write.
        if payload.dry_run:
            audit = self.append_audit(
                "semantic_change_apply_dry_run",
                package.campaign_id,
                dry_run=True,
                details={
                    "package_id": package_id,
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex" if is_live else "mock",
                    "operations_count": len(package.preview.operations),
                },
            )
            result = SemanticChangeApplyResult(
                package_id=package_id,
                campaign_id=package.campaign_id,
                mode=mode,
                dry_run=True,
                applied=False,
                audit_id=audit.id,
                source="yandex" if is_live else "mock",
                operations_sent=0,
            )
            self.semantic_apply_results_by_key[cache_key] = result
            return result

        # Real apply: gate by mode. live_readonly / sandbox are blocked
        # before any network call so the rejection is guaranteed to be
        # no-network.
        if not can_write:
            raise YandexDirectError(
                "Live writes are not allowed in live_readonly mode; "
                "switch DIRECTPILOT_MODE to live_write to apply semantic changes"
            )
        if client is None:
            raise YandexDirectError(
                "YandexDirectClient is required for live semantic-change writes"
            )

        # Send the prepared operations. Each operation is routed to the
        # matching explicit helper on YandexDirectClient. We never call
        # the private ``_call`` from the store. On the first error we
        # record a ``semantic_change_apply_failed`` audit event with no
        # token leakage and re-raise so the endpoint can return 502.
        #
        # Special case for ``adgroups.update``: Direct's
        # ``NegativeKeywords.Items`` field is REPLACE, not APPEND. To
        # avoid silently wiping the live group's existing negatives, the
        # apply path first reads the current negatives via
        # ``adgroups.get`` and merges them with the requested phrases
        # (order-preserving, de-duplicated). If the read fails or the
        # target ad group id is not found in the response, we abort
        # BEFORE the update and audit ``semantic_change_apply_failed``.
        sent_units = 0
        try:
            for operation in package.preview.operations:
                method = operation.method
                params = operation.params
                if method == "keywords.add":
                    response = client.keywords_add(params.get("Keywords", []))
                elif method == "adgroups.update":
                    response = self._apply_with_existing_negatives(
                        client=client,
                        campaign_id=package.campaign_id,
                        params=params,
                    )
                else:
                    # Defensive: every operation built by
                    # ``_build_semantic_operations`` is one of the two
                    # above. If something else slipped in (e.g. via a
                    # future migration that hand-edits a package), fail
                    # closed.
                    raise YandexDirectError(
                        f"Unsupported semantic-change operation: {method!r}"
                    )
                if not response.get("ok"):
                    err = response.get("error") or {}
                    raise YandexDirectError(
                        f"Yandex Direct rejected {method}: "
                        f"error_code={err.get('error_code')!r}"
                    )
                sent_units += int(response.get("units") or 0)
        except YandexDirectError as exc:
            self.append_audit(
                "semantic_change_apply_failed",
                package.campaign_id,
                dry_run=False,
                details={
                    "package_id": package_id,
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "yandex_error": str(exc),
                },
            )
            raise

        audit = self.append_audit(
            "semantic_change_applied",
            package.campaign_id,
            dry_run=False,
            details={
                "package_id": package_id,
                "approved": payload.approved,
                "idempotency_key": payload.idempotency_key,
                "mode": mode,
                "source": "yandex",
                "operations_count": len(package.preview.operations),
                "yandex_units": sent_units,
                # Surface the merge-on-apply marker and the live-group
                # negative count that was read in front of the update,
                # so the audit log shows the effective payload (not just
                # the requested one). Stays pure-local when
                # merge_on_apply is False.
                "merge_on_apply": bool(package.preview.merge_on_apply),
            },
        )
        package.status = "applied"
        result = SemanticChangeApplyResult(
            package_id=package_id,
            campaign_id=package.campaign_id,
            mode=mode,
            dry_run=False,
            applied=True,
            audit_id=audit.id,
            source="yandex",
            operations_sent=len(package.preview.operations),
        )
        self.semantic_apply_results_by_key[cache_key] = result
        return result


store = MockStore()
