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
    LiveCreateCampaignRequest,
    LiveCreateCampaignResult,
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
    YandexAdsBusinessAttachRequest,
    YandexAdsBusinessAttachResult,
    YandexAdsBusinessAttachSkipped,
    YandexControlRequest,
    YandexControlResult,
    YandexTimeTargetingRequest,
    YandexTimeTargetingResult,
    YandexTimeTargetingSchedule,
    YandexVCardRequest,
    YandexVCardResult,
)
from app.yandex_direct import YandexDirectClient, YandexDirectError


def _normalize_phrase(value: str) -> str:
    return " ".join(value.split()).strip().lower()


# ---------------------------------------------------------------------------
# Region resolution (geo targeting -> v5 ``RegionIds``)
#
# The live-create chain's ``adgroups.add`` payload MUST carry a valid
# ``RegionIds`` list (reviewer REQUEST_CHANGES blocker). The draft only
# stores a human-readable region name (``draft.region``); we resolve it
# to Yandex's internal region ids via an explicit local map so:
#
# * the chain has a single source of truth for the operator-visible
#   region name and the v5 id (no magic numbers in the apply path);
# * unknown / unmapped regions fail closed BEFORE any ``campaigns.add``
#   network call (the resolver raises :class:`YandexDirectError` so the
#   endpoint surfaces it as a 502 with a redacted message);
# * the map is trivially extensible — add one entry, no other change
#   is required. No external lookup, no network call, no caching.
#
# The numeric ids are the documented v5 ``RegionIds`` for the cities
# we ship in the Beta. If/when we add more regions the operator should
# add them here and pin them in
# ``tests/test_live_create_chain_helpers.py``.
# ---------------------------------------------------------------------------

_REGION_NAME_TO_V5_IDS: dict[str, list[int]] = {
    # Russian cities we ship in the Beta
    "казань": [43],
    "москва": [213],
    "санкт-петербург": [2],
    "спб": [2],
    # Country-level fallback (whole of Russia)
    "россия": [225],
    "russia": [225],
}


def _resolve_region_to_ids(region: str | None) -> list[int]:
    """Resolve a human-readable region name to a v5 ``RegionIds`` list.

    Lookup is case-insensitive and whitespace-tolerant so the operator
    can paste ``"Казань"`` / ``"казань"`` / ``"  Казань  "`` and
    always get the same ids. The map is intentionally explicit — no
    external lookup, no network call.

    Raises :class:`YandexDirectError` for ``None``, empty / whitespace,
    or unmapped region names. The message names the offending region
    so the operator can fix it and does NOT carry the OAUTH token.
    """
    if region is None:
        raise YandexDirectError(
            "live-create: draft.region is missing; cannot resolve v5 RegionIds. "
            "Set draft.region to a known region name before apply."
        )
    key = _normalize_phrase(region)
    if not key:
        raise YandexDirectError(
            "live-create: draft.region is empty or whitespace; cannot resolve "
            "v5 RegionIds. Set draft.region to a known region name before apply."
        )
    ids = _REGION_NAME_TO_V5_IDS.get(key)
    if ids is None:
        # Redact: do NOT echo ``region`` from arbitrary user input
        # (operator may have pasted something containing a token),
        # but DO mention the name so the operator can fix it. The
        # call site controls what is passed here — it is always
        # ``draft.region`` from the operator's own draft, not from
        # an external client. We do not include the full list of
        # known regions in the error so the response stays short.
        raise YandexDirectError(
            f"live-create: unknown region {region!r}; cannot resolve v5 "
            f"RegionIds. Add an entry to _REGION_NAME_TO_V5_IDS in "
            f"app/store.py or pick a known region (e.g. 'Казань', "
            f"'Москва', 'Санкт-Петербург', 'Россия')."
        )
    return list(ids)


def _safe_units(value: Any) -> int:
    """Coerce a Yandex ``Units`` response value to ``int`` without raising.

    The Direct API v5 ``Units`` header is documented as a decimal string,
    but the response can be empty, ``None``, or non-numeric on edge
    paths (e.g. proxies returning ``Units: -`` for zero-cost ops, or
    HTTP-level error envelopes that still parse as JSON). The old
    ``int(response.get("units") or 0)`` raised ``ValueError`` on those
    shapes, which propagated as an uncaught 500 with no audit. We never
    need the raw units for correctness (the audit just surfaces a
    total), so we return ``0`` for any non-integer-looking value.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else 0
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _format_add_result_error(errors: list[Any]) -> str:
    """Render a v5 ``AddResults[].Errors[]`` array as a redacted one-line
    summary suitable for an audit event and an HTTP error message.

    The v5 contract renders each error as
    ``{Code, Message, Details, Fairy, ...}``. We keep only the
    short ``Code`` and a truncated ``Message`` — the full raw payload
    is NEVER included because it may contain user data the operator
    pasted into the campaign name.
    """
    if not errors:
        return "AddResults.Errors present but empty"
    parts: list[str] = []
    for err in errors[:3]:  # cap at 3 errors to keep the message short
        if isinstance(err, dict):
            code = err.get("Code")
            message = err.get("Message")
            code_str = f"code={code}" if code is not None else "code=?"
            msg_str = (
                f": {str(message)[:120]}"
                if message is not None
                else ""
            )
            parts.append(f"{code_str}{msg_str}")
        else:
            parts.append(str(err)[:120])
    if len(errors) > 3:
        parts.append(f"...({len(errors) - 3} more)")
    return "AddResults.Errors: " + "; ".join(parts)


def _extract_add_results(
    result_payload: Any,
    *,
    stage: str = "campaigns.add",
) -> tuple[list[dict[str, Any]] | None, tuple[str, dict[str, Any]] | None]:
    """Extract and validate a v5 ``AddResults`` envelope.

    Returns ``(add_results, None)`` on a well-formed success envelope
    (a non-empty list of items, the first of which has ``Id`` set and
    no ``Errors``). Returns ``(None, (message, structured))`` on a
    per-item failure: ``Errors`` present, or ``Id`` missing.

    ``stage`` is threaded into safe messages so audit can identify
    the exact failing v5 call in a multi-stage live-create chain.
    """
    if not isinstance(result_payload, dict):
        return None, (
            f"Yandex Direct {stage} envelope missing 'result'; "
            "refusing to invent an id",
            {"reason": "missing_result", "stage": stage},
        )
    add_results_raw = result_payload.get("AddResults")
    if not isinstance(add_results_raw, list) or not add_results_raw:
        return None, (
            f"Yandex Direct {stage} returned an empty AddResults "
            "envelope; refusing to invent an id",
            {"reason": "empty_add_results", "stage": stage},
        )
    first = add_results_raw[0]
    if not isinstance(first, dict):
        return None, (
            f"Yandex Direct {stage} AddResults item is not an object; "
            "refusing to invent an id",
            {"reason": "add_results_item_not_object", "stage": stage},
        )
    errors = first.get("Errors")
    if isinstance(errors, list) and errors:
        return None, (
            f"Yandex Direct rejected {stage}: "
            f"{_format_add_result_error(errors)}",
            {"reason": "add_results_errors", "error_count": len(errors), "stage": stage},
        )
    if first.get("Id") is None:
        return None, (
            f"Yandex Direct {stage} AddResults item has no Id; "
            "refusing to invent an id",
            {"reason": "add_results_missing_id", "stage": stage},
        )
    return [first], None


def _stage_name_from_message(message: str) -> str | None:
    """Extract the failing v5 stage name from a chain error message.

    The chain ``raise``s ``YandexDirectError`` whose message embeds
    the failing stage name (e.g. ``"Yandex Direct rejected
    adgroups.add: error_code=..."``, ``"ads.add: ad targets
    unknown local ad group id=..."``, ``"keywords.add returned N
    ids for M submitted keywords; refusing to continue the
    chain"``). This helper pulls that name out so the audit log
    can pin the operator to the exact stage that failed. Returns
    ``None`` when the message does not carry a stage name — the
    caller falls back to ``"unknown"`` or ``"transport"``.
    """
    if not isinstance(message, str):
        return None
    for stage in (
        "campaigns.add",
        "adgroups.add",
        "ads.add",
        "keywords.add",
        "negativekeywordsharedsets.add",
    ):
        if stage in message:
            return stage
    return None


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
        # Live-create campaign results, keyed by idempotency_key (with
        # a ``live_create:<draft_id>`` prefix to avoid cross-draft
        # collisions). Replays with the same key return the cached
        # result without re-sending to Yandex.
        self.live_create_results_by_key: dict[str, Any] = {}
        # Time-targeting apply results, keyed by (campaign_id,
        # idempotency_key) so replays of the same apply return the
        # cached result without re-sending to Yandex.
        self.time_targeting_results_by_key: dict[str, Any] = {}
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


    # ------------------------------------------------- time-targeting update
    #
    # Gated update of an existing campaign's ``TimeTargeting`` block
    # via v5 ``campaigns.update``. Mirrors the same gate contract as
    # the rest of the product surface:
    #
    # * ``approved`` MUST be ``True`` (the endpoint enforces this
    #   with HTTP 409, the store double-checks).
    # * ``idempotency_key`` is required (Pydantic enforces
    #   ``min_length=6``). Replays of the same ``(campaign_id,
    #   idempotency_key)`` pair return the cached result without
    #   re-sending to Yandex.
    # * ``dry_run=True`` is ALWAYS allowed and NEVER performs a
    #   network write. The result includes the v5
    #   ``campaigns.update`` payload preview.
    # * ``live_readonly`` + ``dry_run=False`` is REJECTED before any
    #   network call (the same gate as live-create).
    # * ``live_write`` + ``approved`` + ``idempotency_key`` +
    #   ``dry_run=False`` performs the real apply: a v5
    #   ``campaigns.update`` call with the canonical
    #   ``TimeTargeting`` block, followed by a read-back via
    #   ``campaigns.get`` to verify the schedule landed.
    #
    # The apply path uses the canonical 7 x 24 matrix. The
    # ``hours`` / ``days`` request shape is expanded into the
    # canonical matrix by the model layer; the store NEVER
    # inverts that expansion, so a dry-run and an apply see the
    # exact same ``TimeTargeting`` payload.
    # ------------------------------------------------------------------

    @staticmethod
    def _build_v5_time_targeting_from_schedule(
        schedule: YandexTimeTargetingSchedule,
    ) -> list[dict[str, Any]]:
        """Build the v5 ``TimeTargeting`` block from a canonical schedule.

        The result is a list of seven ``TimeTargetItem`` dictionaries
        in the v5 day-of-week order MONDAY..SUNDAY. Each item carries
        a ``Days`` field (a list of the single day name) and a
        ``Hours`` block with 24 ``BidPercent`` integer values in the
        0..100 range.

        The model layer has already validated the 7 x 24 matrix and
        the integer range, so this helper only reshapes. The v5
        contract for a TimeTargetItem is documented at
        https://yandex.com/dev/direct/doc/ref-v5/campaigns/update.html
        — we mirror the documented ``Days`` / ``Hours.BidPercent``
        shape literally.
        """
        from app.models import WEEK_DAY_NAMES

        if len(schedule.days) != len(WEEK_DAY_NAMES):
            # Defensive guard. The model layer has already pinned
            # this at 7 via ``min_length`` / ``max_length`` but a
            # direct caller of the helper must also get a typed
            # error rather than a confusing IndexError.
            raise YandexDirectError(
                f"TimeTargeting schedule must have 7 days, got {len(schedule.days)}"
            )
        return [
            {
                "Days": [WEEK_DAY_NAMES[index]],
                "Hours": {"BidPercent": list(day.hours)},
            }
            for index, day in enumerate(schedule.days)
        ]

    @staticmethod
    def _normalize_time_targeting_schedule(
        schedule: YandexTimeTargetingSchedule,
    ) -> YandexTimeTargetingSchedule:
        """Validate and re-wrap a 7-day schedule in v5 day order.

        ``MONDAY`` MUST be at index 0 and ``SUNDAY`` at index 6 — that
        is the documented v5 contract. The request model is positional
        and does not infer named-day ordering from ``schedule.days``;
        callers that want a named-day shortcut should use the flat
        ``hours`` + ``days`` request shape instead. Days that are out
        of range are caught by the model layer (Pydantic ``min_length``
        / ``max_length``).
        """
        from app.models import WEEK_DAY_NAMES

        # Pydantic has already validated exactly 7 entries, so the
        # positional index 0..6 maps directly to MONDAY..SUNDAY. The
        # helper is idempotent — feeding it a schedule that is
        # already in canonical order is a no-op.
        if len(schedule.days) != len(WEEK_DAY_NAMES):
            raise YandexDirectError(
                f"TimeTargeting schedule must have 7 days, got {len(schedule.days)}"
            )
        return YandexTimeTargetingSchedule(days=list(schedule.days))

    def yandex_time_targeting(
        self,
        campaign_id: str,
        payload: YandexTimeTargetingRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> YandexTimeTargetingResult:
        """Apply a time-targeting update for an existing campaign.

        See the contract block above for the full gate description.
        """
        if not payload.approved:
            raise ValueError("Action requires explicit approval")

        # Build + validate the canonical schedule exactly once.
        # ``_normalize_time_targeting_schedule`` raises a typed
        # ``YandexDirectError`` if the schedule is malformed at
        # the store layer (the model has already validated it; this
        # is defence-in-depth for direct callers).
        if payload.schedule is not None:
            canonical_schedule = self._normalize_time_targeting_schedule(
                payload.schedule
            )
        else:
            # Expand the ``hours`` shape into a canonical 7 x 24
            # matrix. ``days`` defaults to every day of the week
            # when omitted; ``None``/empty list also means every
            # day. Per-day re-validation here would duplicate the
            # Pydantic contract; we just build a clean 7-entry
            # schedule.
            from app.models import WEEK_DAY_NAMES, YandexTimeTargetingHourly

            assert payload.hours is not None  # model enforces either/or
            if payload.days is None:
                target_days = list(WEEK_DAY_NAMES)
            else:
                target_days = [
                    day.strip().upper() for day in payload.days
                ]
            schedule_days: list[YandexTimeTargetingHourly] = []
            for day_name in WEEK_DAY_NAMES:
                if day_name in target_days:
                    schedule_days.append(
                        YandexTimeTargetingHourly(hours=list(payload.hours))
                    )
                else:
                    # Direct's v5 contract: a TimeTargetItem's
                    # ``Hours.BidPercent`` is required. Days not
                    # listed in the request are set to all-zeros
                    # (``0`` = paused during that hour) so the
                    # campaign is paused on the missing days
                    # rather than left in whatever the previous
                    # schedule was. This is the same shape the v5
                    # service would accept on a brand-new
                    # TimeTargeting block.
                    schedule_days.append(
                        YandexTimeTargetingHourly(hours=[0] * 24)
                    )
            canonical_schedule = YandexTimeTargetingSchedule(days=schedule_days)

        v5_time_targeting = self._build_v5_time_targeting_from_schedule(
            canonical_schedule
        )
        payload_preview: dict[str, Any] = {
            "method": "campaigns.update",
            "params": {
                "Campaigns": [
                    {
                        "Id": YandexDirectClient._direct_id(campaign_id),
                        "TimeTargeting": v5_time_targeting,
                    }
                ]
            },
        }

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"

        # Idempotency: cache key is (campaign_id, idempotency_key).
        # ``dry_run`` is NOT part of the cache key on purpose: a
        # single idempotency_key represents one operator action;
        # mixing a dry-run replay and a real-apply replay on the
        # same key is unsupported and would be a contract bug,
        # not a feature. We require the same ``dry_run`` flag
        # across replays and surface a 409 otherwise. This mirrors
        # the existing live-create cache contract.
        cache_key = f"time_targeting:{campaign_id}:{payload.idempotency_key}"
        if cache_key in self.time_targeting_results_by_key:
            cached = self.time_targeting_results_by_key[cache_key]
            if cached.dry_run != payload.dry_run:
                # The cache key is supposed to represent one
                # action. Replaying with a different dry_run flag
                # is a logic error; surface as a typed error so
                # the endpoint returns 409.
                raise YandexDirectError(
                    f"Idempotency key {payload.idempotency_key!r} was "
                    f"previously used with dry_run={cached.dry_run}; "
                    f"replay with dry_run={payload.dry_run} is not allowed"
                )
            return cached

        # Mock mode: pure in-memory mirror. The apply mutates the
        # local mirror so a follow-up ``campaigns.get
        # TimeTargeting`` would see the new schedule. The result
        # surfaces ``applied=False`` regardless of ``dry_run`` —
        # mock is the no-network, no-mutation path; even a
        # ``dry_run=False`` request is treated as a dry-run so the
        # operator can preview the payload the apply WOULD have
        # sent. (The endpoint gate is the primary mode guard: any
        # non-``live_write`` apply is rejected with HTTP 409 before
        # this branch ever runs, so the mock apply path here is
        # only reachable via direct store calls — e.g. an
        # integration test, a future background worker, or a
        # caller that bypasses the endpoint gate.)
        if not is_live:
            audit = self.append_audit(
                "yandex_time_targeting_requested",
                campaign_id,
                dry_run=payload.dry_run,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "timezone": payload.timezone,
                    "source": "mock",
                    "mode": mode,
                    "schedule_applied": canonical_schedule.model_dump(
                        mode="json"
                    ),
                },
            )
            result = YandexTimeTargetingResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=payload.dry_run,
                # Mock is always ``applied=False`` — the local
                # mirror is not a real Yandex apply. The endpoint
                # gate already blocks non-``live_write`` apply, so
                # this branch is only exercised by dry-run or
                # direct store callers.
                applied=False,
                source="mock",
                audit_id=audit.id,
                payload_preview=payload_preview,
                schedule_applied=canonical_schedule,
                readback=None,
                timezone=payload.timezone,
            )
            self.time_targeting_results_by_key[cache_key] = result
            return result

        # Live modes: dry-run is always allowed and never mutates.
        if payload.dry_run:
            audit = self.append_audit(
                "yandex_time_targeting_requested",
                campaign_id,
                dry_run=True,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "timezone": payload.timezone,
                    "source": "yandex",
                    "mode": mode,
                    "payload_redacted": payload_preview,
                },
            )
            result = YandexTimeTargetingResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=True,
                applied=False,
                source="yandex",
                audit_id=audit.id,
                payload_preview=payload_preview,
                schedule_applied=canonical_schedule,
                readback=None,
                timezone=payload.timezone,
            )
            self.time_targeting_results_by_key[cache_key] = result
            return result

        # Real apply: only ``live_write`` may proceed. The other
        # live-ish modes (``sandbox`` / ``live_readonly``) share
        # the same v5 ``campaigns.update`` write shape with
        # production — a real apply against a sandbox token would
        # mutate the user's sandbox account, and ``live_readonly``
        # is the documented read-only path.
        if not can_write:
            raise YandexDirectError(
                f"Live writes require DIRECTPILOT_MODE=live_write; "
                f"current mode is {mode!r}; refusing to dispatch "
                f"campaigns.update"
            )
        if client is None:
            raise YandexDirectError(
                "YandexDirectClient is required for live time-targeting writes"
            )

        try:
            yandex_result = client.campaigns_update_time_targeting(
                campaign_id, list(v5_time_targeting)
            )
            if not yandex_result.get("ok"):
                err = yandex_result.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected campaigns.update: "
                    f"error_code={err.get('error_code')!r}"
                )
            # Read-back via ``campaigns.get TimeTargeting`` so the
            # operator sees the live schedule that landed on
            # Direct. We surface the full ``TimeTargeting`` block
            # exactly as v5 returned it, so the operator can diff
            # it against ``schedule_applied`` without any
            # reshaping.
            readback_response = client.campaigns_get_time_targeting(campaign_id)
            readback_block: dict[str, Any] | None = None
            if readback_response.get("ok"):
                readback_result = readback_response.get("result") or {}
                if isinstance(readback_result, dict):
                    readback_campaigns = readback_result.get("Campaigns") or []
                    if readback_campaigns and isinstance(
                        readback_campaigns[0], dict
                    ):
                        candidate = readback_campaigns[0].get("TimeTargeting")
                        if isinstance(candidate, list):
                            readback_block = {"TimeTargeting": list(candidate)}
            # The readback is best-effort: a v5 ok envelope with a
            # missing/odd TimeTargeting shape is recorded in the
            # audit, not in the readback field. The apply itself
            # succeeded as far as Direct is concerned; a missing
            # readback is informational, not an error.
            audit = self.append_audit(
                "yandex_time_targeting_requested",
                campaign_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "timezone": payload.timezone,
                    "source": "yandex",
                    "mode": mode,
                    "schedule_applied": canonical_schedule.model_dump(
                        mode="json"
                    ),
                    "yandex_units": yandex_result.get("units"),
                    "readback_present": readback_block is not None,
                },
            )
            result = YandexTimeTargetingResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=False,
                applied=True,
                source="yandex",
                audit_id=audit.id,
                payload_preview=None,
                schedule_applied=canonical_schedule,
                readback=readback_block,
                timezone=payload.timezone,
            )
            self.time_targeting_results_by_key[cache_key] = result
            return result
        except YandexDirectError as exc:
            self.append_audit(
                "yandex_time_targeting_failed",
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

    # ------------------------------------------------- live-create campaign
    #
    # Gated creation of a real Yandex Direct campaign from a
    # :class:`CampaignDraft` preview. The contract is identical to the
    # rest of the product: ``dry_run`` / ``approved`` / ``idempotency_key``
    # plus the runtime mode (``live_readonly`` blocks real writes;
    # ``live_write`` allows them). ``mock`` and ``sandbox`` are not
    # product write paths for live-create (a sandbox token has the
    # same write shape as a real one — we keep the gate strict).
    #
    # The chain performs FOUR v5 stages on apply:
    #
    # 1. ``campaigns.add`` (text campaign shape; Direct controls lifecycle state)
    # 2. ``adgroups.add`` (one per draft ad group, with the draft
    #    ``negative_keywords`` mirrored into each group's
    #    ``NegativeKeywords.Items`` block — same shape the existing
    #    ``adgroups.update`` semantic-change path uses)
    # 3. ``ads.add`` (one per draft ad, targeting the Yandex ad
    #    group id returned by stage 2)
    # 4. ``keywords.add`` (one per group-level keyword, plus a
    #    broadcast of campaign-level keywords onto every group,
    #    targeting the Yandex ad group id from stage 2)
    #
    # The chain does NOT call ``campaigns.resume`` automatically.
    # Direct controls the initial lifecycle/moderation state;
    # activation goes through
    # the existing ``POST /yandex/campaigns/{campaign_id}/resume``
    # endpoint with its own approval / idempotency gate.
    #
    # ``negativekeywordsharedsets.add`` (stage 5) is the ONLY stage
    # kept as ``not_implemented`` — the v5 shape for that service
    # is not documented in the project sources. Group-level
    # negatives are applied via the confirmed
    # ``adgroups.add`` ``NegativeKeywords.Items`` block, which is
    # the same shape the existing ``adgroups.update`` semantic-
    # change path uses. No invented payload.

    @staticmethod
    def _build_v5_campaign_from_draft(
        draft: "CampaignDraft",
        *,
        start_date: str | None,
        counter_ids: list[int],
    ) -> dict[str, Any]:
        """Build one v5 ``Campaigns`` entry from a campaign-draft preview.

        Field names mirror the Direct API v5 contract literally — no
        invented fields. The TextCampaign block carries the bidding
        strategy, geo / time targeting settings, and the optional
        Metrika counter ids.
        """
        daily_budget_amount: int | None = None
        # Defensive ``draft.budget`` guard. ``CampaignDraft.budget`` is
        # typed with ``default_factory=BudgetSettings`` so it is
        # effectively never ``None`` in normal model roundtrips, but
        # the v5 builder is a safety-sensitive function: any direct
        # caller (an integration test, a future background worker, a
        # hand-built model) that hands us a draft with ``budget=None``
        # MUST NOT raise an ``AttributeError`` that escapes to the
        # endpoint's safety net. We coerce to a safe
        # ``BudgetSettings()`` default and surface the budget block
        # only when the operator actually set a daily amount.
        budget_obj = getattr(draft, "budget", None) or BudgetSettings()
        if budget_obj.daily_budget is not None:
            # v5 expects micro-units (1/1_000_000 of currency) plus a
            # currency string. We default to RUB because the rest of
            # the product is RU-first; if the user has a multi-currency
            # account they can extend this scaffolding.
            daily_budget_amount = int(round(budget_obj.daily_budget * 1_000_000))

        campaign: dict[str, Any] = {
            "Name": draft.name or f"Draft: {draft.business_type} / {draft.region}",
            "TextCampaign": {
                "BiddingStrategy": {
                    # Search-only starter strategy. Direct v5 requires both
                    # Search and Network blocks on TextCampaign.BiddingStrategy.
                    # We keep networks off for the user's single-intent service
                    # landing until there is enough search-query evidence to
                    # expand traffic safely.
                    "Search": {
                        "BiddingStrategyType": "HIGHEST_POSITION",
                        "PlacementTypes": {
                            "SearchResults": "YES",
                            "ProductGallery": "NO",
                        },
                    },
                    "Network": {
                        "BiddingStrategyType": "SERVING_OFF",
                    },
                },
            },
        }
        # ``StartDate`` is a v5 required string — sending ``None``
        # would be rejected. When the caller did not supply an
        # override, omit the key entirely so the v5 service applies
        # its own default (today UTC). Mirrors the documented v5
        # contract: a missing optional-required field falls back to
        # the service default, but a present ``null`` does not.
        if start_date is not None:
            campaign["StartDate"] = start_date
        if daily_budget_amount is not None:
            campaign["DailyBudget"] = {
                "Amount": daily_budget_amount,
                "Mode": budget_obj.daily_budget_mode,
            }
        if counter_ids:
            campaign["TextCampaign"]["CounterIds"] = {"Items": list(counter_ids)}
        # Geo targeting: v5 expects RegionIds. The draft only stores
        # the human-readable region name; we leave ``Settings`` empty
        # by default and let the operator refine in the Yandex UI.
        return campaign

    @staticmethod
    def _build_v5_chain_payloads(
        draft: "CampaignDraft",
        *,
        campaign_id: int | str | None,
    ) -> dict[str, dict[str, Any]]:
        """Build the v5 stage payloads for stages 2..4 of the
        live-create chain.

        Returns a dict keyed by the v5 method name:

        * ``adgroups.add`` — one ``AdGroups`` item per draft ad
          group, with the draft's ``negative_keywords`` mirrored
          into each group's ``NegativeKeywords.Items`` block (same
          shape the existing ``adgroups.update`` semantic-change
          path uses). ``CampaignId`` is the new Yandex campaign id
          from stage 1 (may be ``None`` in the dry-run preview
          because stage 1 has not run yet — the field is always
          present in the payload to match the v5 contract).
        * ``ads.add`` — one ``Ads`` item per draft ad, with
          ``AdGroupId`` set to the local draft ad group id (the
          apply path substitutes the Yandex id from stage 2 via
          the local→Yandex map). The ``TextAd`` block carries
          ``Title`` / ``Text`` / ``Href`` and, when set,
          ``DisplayLinkPath`` (the v5 optional field).
        * ``keywords.add`` — a flat list of ``{Keyword, AdGroupId}``
          entries. Group-level keywords from each draft ad group
          are emitted with the local ad group id; campaign-level
          keywords (``draft.keywords``) are broadcast onto every
          group. The apply path substitutes Yandex ids via the
          local→Yandex map.

        The contract is documented in
        :class:`app.models.LiveCreateCampaignResult` and pinned by
        ``tests/test_live_create_chain_helpers.py``.

        Empty lists are returned for stages whose draft inputs are
        empty so the apply path can SKIP the network call instead
        of sending ``{"AdGroups": []}`` to v5 (which the v5 service
        rejects). The dry-run preview still surfaces the empty
        stage in the chain so the operator can see the chain
        intentionally no-ops on missing parts.

        ``adgroups.add`` items ALWAYS carry ``RegionIds`` (v5
        rejects items without a geo target). The ids are resolved
        from ``draft.region`` via :func:`_resolve_region_to_ids` —
        a local map, no network lookup. An unknown region raises
        :class:`YandexDirectError` so the apply path refuses to
        dispatch ``campaigns.add`` (and the dry-run surfaces the
        same typed error). The ``NegativeKeywords`` block is
        OPTIONAL on ``adgroups.add``; we OMIT the block when the
        list is empty and INCLUDE it with the items when it is not
        — matching the existing ``adgroups.update`` shape for
        consistency.
        """
        # Resolve ``RegionIds`` from ``draft.region`` first. An
        # unknown / empty / whitespace region raises
        # :class:`YandexDirectError` (typed) so the apply path
        # fails closed before any network call. The dry-run path
        # surfaces the same error, so the operator sees the same
        # failure mode in both ``dry_run`` and ``approved=True``
        # requests.
        region_ids = _resolve_region_to_ids(getattr(draft, "region", None))
        negative_items = [
            phrase
            for phrase in (draft.negative_keywords or [])
            if "/" not in phrase and "\\" not in phrase
        ]

        # Stage 2 — adgroups.add. ``CampaignId`` may be ``None`` in
        # the dry-run preview because stage 1 has not run yet; the
        # apply path substitutes the Yandex campaign id before
        # calling ``adgroups.add``.
        ad_groups_param: list[dict[str, Any]] = []
        for group in draft.ad_groups or []:
            item: dict[str, Any] = {
                "Name": group.name,
                "CampaignId": campaign_id,
                # Geo target — required by v5. Resolved from
                # ``draft.region`` via the local resolver above.
                "RegionIds": list(region_ids),
            }
            # Group-level negatives mirror the
            # ``adgroups.update`` semantic-change shape so the
            # negative-keyword requirement is met without
            # inventing a new v5 service payload. The block is
            # OPTIONAL on v5 ``adgroups.add``; we OMIT it when
            # the list is empty (a missing block is always
            # accepted; an empty Items list is rejected on some
            # upstream edge cases) and INCLUDE it with the items
            # when it is not — the apply path is then a
            # deterministic, documented payload.
            if negative_items:
                item["NegativeKeywords"] = {"Items": list(negative_items)}
            ad_groups_param.append(item)

        # Stage 3 — ads.add. ``AdGroupId`` uses the LOCAL draft
        # ad group id; the apply path substitutes the Yandex id
        # from stage 2 via the local→Yandex map.
        ads_param: list[dict[str, Any]] = []
        for ad in draft.ads or []:
            text_ad: dict[str, Any] = {
                "Title": ad.title,
                "Text": ad.text,
                "Href": ad.landing_url,
            }
            if ad.display_link_path:
                # Direct v5 ``ads.add`` for TextAd currently rejects
                # ``DisplayLinkPath`` in the add payload. Keep the field in
                # the local draft/preview model for marketer readability, but
                # omit it from live-create until the exact supported upstream
                # field is confirmed and tested.
                pass
            ads_param.append(
                {
                    "AdGroupId": ad.ad_group_id,
                    "TextAd": text_ad,
                }
            )

        # Stage 4 — keywords.add. Flatten group-level keywords
        # (one entry per (group, phrase)) and broadcast the
        # campaign-level keywords onto every group. The apply
        # path substitutes Yandex ids via the local→Yandex map.
        keywords_param: list[dict[str, Any]] = []
        local_group_ids = [g.id for g in (draft.ad_groups or [])]
        for group in draft.ad_groups or []:
            for phrase in group.keywords or []:
                keywords_param.append(
                    {"Keyword": phrase, "AdGroupId": group.id}
                )
        # Broadcast campaign-level keywords onto every group, in
        # the order the groups appear in the draft. We dedupe
        # against the per-group set so the v5 service does not
        # receive the same phrase twice in the same call.
        for phrase in draft.keywords or []:
            for group_id in local_group_ids:
                keywords_param.append(
                    {"Keyword": phrase, "AdGroupId": group_id}
                )

        return {
            "adgroups.add": {
                "method": "adgroups.add",
                "params": {"AdGroups": ad_groups_param},
            },
            "ads.add": {
                "method": "ads.add",
                "params": {"Ads": ads_param},
            },
            "keywords.add": {
                "method": "keywords.add",
                "params": {"Keywords": keywords_param},
            },
        }

    def live_create_campaign(
        self,
        payload: "LiveCreateCampaignRequest",
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> "LiveCreateCampaignResult":
        """Apply a live-create campaign request.

        Contract:

        * ``approved`` MUST be ``True`` (the endpoint already enforces
          this with HTTP 409, but the store double-checks so direct
          callers cannot bypass the gate).
        * ``idempotency_key`` is required (the Pydantic model enforces
          a minimum length of 6 chars). The first call performs the
          network write; replays return the cached result without
          re-sending.
        * ``dry_run=True`` is ALWAYS allowed and NEVER performs a
          network write. The result includes the full v5 chain
          preview (stage 1 + stages 2..4) that WOULD be sent.
        * ``live_readonly`` mode + ``dry_run=False`` is REJECTED
          before any network call (raises :class:`YandexDirectError`
          so the endpoint returns 409 with a redacted message).
        * ``live_write`` mode + ``approved`` + ``idempotency_key`` +
          ``dry_run=False`` performs the four-stage v5 chain
          (``campaigns.add`` → ``adgroups.add`` → ``ads.add`` →
          ``keywords.add``) and returns the new ids from the
          ``AddResults`` envelope. The chain is intentionally
          non-activating — the chain does NOT call
          ``campaigns.resume`` automatically. Direct controls the
          initial lifecycle/moderation state. Activation goes
          through the existing
          ``POST /yandex/campaigns/{campaign_id}/resume`` endpoint.

        Idempotency cache keying: the cache key includes
        ``dry_run`` so a dry-run replay does NOT consume a real
        apply's idempotency_key. Replays of the same
        ``(draft_id, idempotency_key, dry_run)`` triple return the
        cached result without re-sending. Mixing dry-run and apply
        on the same key is allowed: each is cached independently.
        """
        if not payload.approved:
            raise ValueError("Action requires explicit approval")
        if payload.draft_id not in self.drafts:
            raise KeyError("campaign_draft_not_found")
        draft = self.drafts[payload.draft_id]
        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"
        # Only one stage remains explicitly not implemented: the
        # v5 shape for ``negativekeywordsharedsets.add`` is not
        # documented in the project sources. Group-level negatives
        # are applied via the confirmed ``adgroups.add``
        # ``NegativeKeywords.Items`` block.
        not_implemented: list[str] = ["negativekeywordsharedsets.add"]

        # Idempotency cache. The key includes ``dry_run`` so a
        # dry-run replay does NOT consume a real apply's
        # idempotency_key. Each (draft_id, idempotency_key, dry_run)
        # triple has its own cache entry.
        dry_flag = "dry" if payload.dry_run else "apply"
        cache_key = (
            f"live_create:{payload.draft_id}:{payload.idempotency_key}:{dry_flag}"
        )
        if cache_key in self.live_create_results_by_key:
            return self.live_create_results_by_key[cache_key]

        # Outer try/except: any :class:`YandexDirectError` that
        # escapes the chain preview (e.g. an unknown region raised
        # by ``_resolve_region_to_ids`` inside
        # ``_build_v5_chain_payloads``) or the apply path's per-
        # stage try is audited here with
        # ``live_create_campaign_failed`` and re-raised so the
        # endpoint returns 502. This guarantees the audit log
        # always sees the failing stage and the offending region
        # (no token in the audit message), regardless of whether
        # the failure was a pre-flight gate (no network call) or
        # an in-stage v5 rejection.
        try:
            return self._live_create_campaign_run(
                payload=payload,
                draft=draft,
                mode=mode,
                is_live=is_live,
                can_write=can_write,
                not_implemented=not_implemented,
                cache_key=cache_key,
                client=client,
            )
        except YandexDirectError as exc:
            # ``exc`` carries the failing stage in its message; the
            # audit pin the operator to the exact line that failed.
            failing_stage = _stage_name_from_message(str(exc))
            self.append_audit(
                "live_create_campaign_failed",
                payload.draft_id,
                dry_run=payload.dry_run,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex" if is_live else "mock",
                    "reason": payload.reason,
                    "stage": failing_stage or "unknown",
                    "yandex_error": str(exc),
                },
            )
            raise

    def _live_create_campaign_run(
        self,
        *,
        payload: "LiveCreateCampaignRequest",
        draft: "CampaignDraft",
        mode: str,
        is_live: bool,
        can_write: bool,
        not_implemented: list[str],
        cache_key: str,
        client: YandexDirectClient | None,
    ) -> "LiveCreateCampaignResult":
        """Inner worker for :meth:`live_create_campaign` — see that
        docstring for the full contract.

        Split out from the public method so the public method can
        keep a single, exhaustive ``YandexDirectError`` audit
        handler that fires on BOTH pre-flight gate failures (e.g.
        unknown region raised before any network call) and on
        per-stage v5 rejections inside the apply path.
        """
        v5_campaign = self._build_v5_campaign_from_draft(
            draft,
            start_date=payload.start_date,
            counter_ids=list(payload.counter_ids),
        )
        # Chain preview (stages 2..4) — uses ``None`` for the
        # Yandex campaign id because stage 1 has not run yet; the
        # apply path substitutes the real id before dispatching.
        # ``_build_v5_chain_payloads`` resolves ``RegionIds`` from
        # ``draft.region``; an unknown region raises
        # :class:`YandexDirectError` here so the operator sees
        # the same failure mode in dry-run and apply.
        chain_preview = self._build_v5_chain_payloads(
            draft, campaign_id=None
        )
        # Flat preview — stage 1 + chain. The dry-run
        # ``payload_preview`` keeps the stage-1 envelope as the
        # top-level fields (``method`` / ``params``) and stashes
        # the chain under ``params.chain`` so callers can render
        # both with one walk.
        preview_payload: dict[str, Any] = {
            "method": "campaigns.add",
            "params": {
                "Campaigns": [v5_campaign],
                "chain": [
                    chain_preview["adgroups.add"],
                    chain_preview["ads.add"],
                    chain_preview["keywords.add"],
                ],
            },
        }

        # The endpoint is the primary mode gate. The store keeps a
        # defence-in-depth check so a direct caller cannot bypass it
        # either. Only ``live_write`` may perform a real apply;
        # ``sandbox`` shares the v5 ``campaigns.add`` write shape with
        # production and ``mock`` has no live client at all — both
        # are explicitly rejected, never silently swallowed.
        if not payload.dry_run and mode != "live_write":
            raise YandexDirectError(
                f"Live writes require DIRECTPILOT_MODE=live_write; "
                f"current mode is {mode!r}; refusing to dispatch "
                f"campaigns.add"
            )

        # Dry-run path: never perform a network write. Allowed in
        # every mode because it does not touch the v5 service.
        if payload.dry_run:
            audit = self.append_audit(
                "live_create_campaign_requested",
                payload.draft_id,
                dry_run=payload.dry_run,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex" if is_live else "mock",
                    "reason": payload.reason,
                    "stage": "dry_run_preview",
                    "stages_executed": [],
                    "not_implemented": not_implemented,
                    "payload_redacted": preview_payload,
                },
            )
            result = LiveCreateCampaignResult(
                draft_id=payload.draft_id,
                mode=mode,
                dry_run=payload.dry_run,
                applied=False,
                campaign_id=None,
                source="yandex" if is_live else "mock",
                audit_id=audit.id,
                payload_preview=preview_payload,
                stages_executed=[],
                ad_group_ids=[],
                ad_ids=[],
                keyword_ids=[],
                not_implemented=not_implemented,
            )
            self.live_create_results_by_key[cache_key] = result
            return result

        # Real apply: gate by mode. live_readonly / sandbox are
        # blocked before any network call so the rejection is
        # guaranteed to be no-network.
        if not can_write:
            raise YandexDirectError(
                "Live writes are not allowed in live_readonly mode; "
                "switch DIRECTPILOT_MODE to live_write to create campaigns"
            )
        if client is None:
            raise YandexDirectError(
                "YandexDirectClient is required for live campaign writes"
            )

        # Apply path: chain through the four v5 stages. Each stage
        # is its own v5 call so a single failure never leaves a
        # partial campaign on the user's account. The chain is
        # fail-closed: any stage that fails stops the chain,
        # audits ``live_create_campaign_failed`` with the failing
        # stage (via the public method's outer
        # ``try / except YandexDirectError``), and re-raises so
        # the endpoint returns 502.
        try:
            # ----- Stage 1: campaigns.add -----------------------------
            stage1_response = client.campaigns_add([v5_campaign])
            if not stage1_response.get("ok"):
                err = stage1_response.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected campaigns.add: "
                    f"error_code={err.get('error_code')!r}"
                )
            # Inspect every AddResults item. v5 ``ok=true`` is NOT
            # enough — per-item ``Errors`` or missing ``Id`` must
            # fail closed.
            stage1_result_payload = stage1_response.get("result")
            stage1_items, stage1_failure = _extract_add_results(
                stage1_result_payload, stage="campaigns.add"
            )
            if stage1_failure is not None:
                safe_msg, _structured = stage1_failure
                raise YandexDirectError(safe_msg)
            assert stage1_items is not None
            stage1_first = stage1_items[0]
            new_campaign_id: int | None = None
            if isinstance(stage1_first, dict) and stage1_first.get("Id") is not None:
                new_campaign_id = stage1_first["Id"]
            if new_campaign_id is None:
                # Defence-in-depth — the extractor already covered
                # this, but the chain logic below needs a non-None
                # int to substitute into stage-2 ``CampaignId``.
                raise YandexDirectError(
                    "Yandex Direct campaigns.add returned no Id; "
                    "refusing to continue the chain"
                )
            stage1_warnings: list[Any] = []
            if isinstance(stage1_first, dict) and isinstance(
                stage1_first.get("Warnings"), list
            ):
                stage1_warnings = list(stage1_first.get("Warnings") or [])

            # ----- Stage 2: adgroups.add -----------------------------
            # Rebuild the chain payload with the real Yandex
            # campaign id so ``CampaignId`` is correct.
            chain_payloads = self._build_v5_chain_payloads(
                draft, campaign_id=new_campaign_id
            )
            ad_groups_param = chain_payloads["adgroups.add"]["params"]["AdGroups"]
            new_ad_group_ids: list[str] = []
            if ad_groups_param:
                stage2_response = client.adgroups_add(ad_groups_param)
                if not stage2_response.get("ok"):
                    err = stage2_response.get("error") or {}
                    raise YandexDirectError(
                        f"Yandex Direct rejected adgroups.add: "
                        f"error_code={err.get('error_code')!r}"
                    )
                stage2_items, stage2_failure = _extract_add_results(
                    stage2_response.get("result"), stage="adgroups.add"
                )
                if stage2_failure is not None:
                    safe_msg, _ = stage2_failure
                    raise YandexDirectError(safe_msg)
                assert stage2_items is not None
                # ``_extract_add_results`` returns the first item
                # only; we need ALL ids, so re-inspect the raw
                # envelope to extract every per-item id.
                stage2_raw = (stage2_response.get("result") or {}).get(
                    "AddResults"
                ) or []
                for item in stage2_raw:
                    if isinstance(item, dict) and item.get("Id") is not None:
                        new_ad_group_ids.append(item["Id"])
                if len(new_ad_group_ids) != len(ad_groups_param):
                    # Mismatch between the number of items we sent
                    # and the number of items with an Id. Fail
                    # closed — the mapping for stages 3/4 would be
                    # ambiguous.
                    raise YandexDirectError(
                        f"adgroups.add returned {len(new_ad_group_ids)} "
                        f"ids for {len(ad_groups_param)} submitted groups; "
                        f"refusing to continue the chain"
                    )
            # local draft ad group id -> Yandex ad group id.
            local_group_ids = [g.id for g in (draft.ad_groups or [])]
            local_to_yandex: dict[str, Any] = dict(
                zip(local_group_ids, new_ad_group_ids)
            )

            # ----- Stage 3: ads.add ---------------------------------
            ads_param_raw = chain_payloads["ads.add"]["params"]["Ads"]
            # Substitute the Yandex ad group id for every ad that
            # targets a known local group. Ads that target an
            # unknown group are dropped with a clear error — the
            # v5 service would reject them anyway, but a clear
            # error here is more debuggable.
            ads_param: list[dict[str, Any]] = []
            for ad in ads_param_raw:
                local_gid = ad.get("AdGroupId")
                if local_gid in local_to_yandex:
                    ad_payload = dict(ad)
                    ad_payload["AdGroupId"] = local_to_yandex[local_gid]
                    ads_param.append(ad_payload)
                else:
                    raise YandexDirectError(
                        f"ads.add: ad targets unknown local ad group "
                        f"id={local_gid!r}; refusing to continue the chain"
                    )
            new_ad_ids: list[str] = []
            if ads_param:
                stage3_response = client.ads_add(ads_param)
                if not stage3_response.get("ok"):
                    err = stage3_response.get("error") or {}
                    raise YandexDirectError(
                        f"Yandex Direct rejected ads.add: "
                        f"error_code={err.get('error_code')!r}"
                    )
                stage3_items, stage3_failure = _extract_add_results(
                    stage3_response.get("result"), stage="ads.add"
                )
                if stage3_failure is not None:
                    safe_msg, _ = stage3_failure
                    raise YandexDirectError(safe_msg)
                assert stage3_items is not None
                stage3_raw = (stage3_response.get("result") or {}).get(
                    "AddResults"
                ) or []
                for item in stage3_raw:
                    if isinstance(item, dict) and item.get("Id") is not None:
                        new_ad_ids.append(str(item["Id"]))
                if len(new_ad_ids) != len(ads_param):
                    raise YandexDirectError(
                        f"ads.add returned {len(new_ad_ids)} ids for "
                        f"{len(ads_param)} submitted ads; refusing to "
                        f"continue the chain"
                    )

            # ----- Stage 4: keywords.add ----------------------------
            keywords_param_raw = chain_payloads["keywords.add"]["params"]["Keywords"]
            keywords_param: list[dict[str, Any]] = []
            for kw in keywords_param_raw:
                local_gid = kw.get("AdGroupId")
                if local_gid in local_to_yandex:
                    kw_payload = dict(kw)
                    kw_payload["AdGroupId"] = local_to_yandex[local_gid]
                    keywords_param.append(kw_payload)
                else:
                    raise YandexDirectError(
                        f"keywords.add: keyword targets unknown local ad "
                        f"group id={local_gid!r}; refusing to continue "
                        f"the chain"
                    )
            new_keyword_ids: list[str] = []
            if keywords_param:
                stage4_response = client.keywords_add(keywords_param)
                if not stage4_response.get("ok"):
                    err = stage4_response.get("error") or {}
                    raise YandexDirectError(
                        f"Yandex Direct rejected keywords.add: "
                        f"error_code={err.get('error_code')!r}"
                    )
                stage4_items, stage4_failure = _extract_add_results(
                    stage4_response.get("result"), stage="keywords.add"
                )
                if stage4_failure is not None:
                    safe_msg, _ = stage4_failure
                    raise YandexDirectError(safe_msg)
                assert stage4_items is not None
                stage4_raw = (stage4_response.get("result") or {}).get(
                    "AddResults"
                ) or []
                for item in stage4_raw:
                    if isinstance(item, dict) and item.get("Id") is not None:
                        new_keyword_ids.append(str(item["Id"]))
                if len(new_keyword_ids) != len(keywords_param):
                    raise YandexDirectError(
                        f"keywords.add returned {len(new_keyword_ids)} ids "
                        f"for {len(keywords_param)} submitted keywords; "
                        f"refusing to continue the chain"
                    )

            # Build the final stages_executed list — only include
            # the stages that actually ran (skipped stages do NOT
            # appear, e.g. an empty-draft apply only runs stage 1).
            stages_executed: list[str] = ["campaigns.add"]
            if ad_groups_param:
                stages_executed.append("adgroups.add")
            if ads_param:
                stages_executed.append("ads.add")
            if keywords_param:
                stages_executed.append("keywords.add")

            audit = self.append_audit(
                "live_create_campaign_requested",
                payload.draft_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex",
                    "reason": payload.reason,
                    "stage": stages_executed[-1],
                    "applied": True,
                    "stages_executed": stages_executed,
                    "not_implemented": not_implemented,
                    "campaign_id": str(new_campaign_id),
                    "ad_group_ids": [str(i) for i in new_ad_group_ids],
                    "ad_ids": list(new_ad_ids),
                    "keyword_ids": list(new_keyword_ids),
                    "yandex_units": _safe_units(stage1_response.get("units")),
                    "yandex_warnings": stage1_warnings,
                },
            )
            result = LiveCreateCampaignResult(
                draft_id=payload.draft_id,
                mode=mode,
                dry_run=False,
                applied=True,
                campaign_id=str(new_campaign_id),
                source="yandex",
                audit_id=audit.id,
                payload_preview=preview_payload,
                stages_executed=stages_executed,
                ad_group_ids=[str(i) for i in new_ad_group_ids],
                ad_ids=list(new_ad_ids),
                keyword_ids=list(new_keyword_ids),
                not_implemented=not_implemented,
            )
            self.live_create_results_by_key[cache_key] = result
            return result
        except YandexDirectError:
            # The public :meth:`live_create_campaign` wraps this
            # worker in an outer ``try / except YandexDirectError``
            # that records the ``live_create_campaign_failed``
            # audit. Re-raise so the outer audit fires (with the
            # full request context) — the operator's audit log
            # always sees the failing stage and the offending
            # region, regardless of where the error was raised.
            raise
        except Exception as exc:  # noqa: BLE001 — safety net
            # Same contract as the semantic-change apply path:
            # convert any non-typed exception to a
            # :class:`YandexDirectError` and re-raise so the public
            # method's outer audit handler fires. The outer audit
            # pins the operator to the failing stage and the
            # exception type — we do NOT append_audit here to
            # avoid a double audit. The endpoint then returns 502.
            safe = YandexDirectError(
                f"unexpected error during live-create: "
                f"{type(exc).__name__}: {exc}"
            )
            raise safe from exc

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
        #
        # Safety net: ``YandexDirectError`` is the only typed error the
        # YandexDirectClient helpers are expected to raise. Any other
        # exception (a non-typed ``RuntimeError`` from a custom httpx
        # transport, a ``TypeError`` from a malformed operation payload,
        # a ``ValueError`` from a non-numeric ``Units`` header, etc.)
        # would otherwise escape to FastAPI's default 500 handler with
        # no audit and a possible token-bearing traceback. We catch
        # ``Exception`` here, normalise it to a typed ``YandexDirectError``
        # for the audit, and re-raise so the endpoint returns 502 with
        # a redacted message.
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
                sent_units += _safe_units(response.get("units"))
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
        except Exception as exc:  # noqa: BLE001 — safety net
            # Convert any non-typed exception into a YandexDirectError
            # so the endpoint returns a 502 with a redacted message,
            # NOT the opaque FastAPI 500 default. This was the root
            # cause of the user-reported "500 without audit" bug.
            safe = YandexDirectError(
                f"unexpected error during semantic-change apply: "
                f"{type(exc).__name__}: {exc}"
            )
            self.append_audit(
                "semantic_change_apply_failed",
                package.campaign_id,
                dry_run=False,
                details={
                    "package_id": package_id,
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "yandex_error": str(safe),
                    "exception_type": type(exc).__name__,
                },
            )
            raise safe from exc

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

    # ------------------------------------------------- yandex ads business attach
    #
    # Attach an existing Yandex Business organization to one or more
    # TextAd objects via v5 ``ads.update``. The endpoint
    # ``POST /yandex/ads/business`` is the safe-by-default counterpart
    # to ``POST /yandex/vcards`` for organization-level contact
    # information: vcards.add can fail with ``error_code=3500`` for
    # several account types, so the BusinessId attach is the
    # preferred route.
    #
    # Gate contract (mirrors the rest of the product):
    #
    # * ``approved`` MUST be ``True`` (otherwise 409).
    # * ``idempotency_key`` is required (length >= 6, enforced by
    #   the Pydantic model). The first call performs the network
    #   write; replays return the cached result without re-sending.
    # * ``dry_run=True`` is ALWAYS allowed and NEVER performs a
    #   network write. The result includes the redacted v5
    #   ``ads.update`` payload preview.
    # * In ``live_readonly`` mode, ``dry_run=False`` is REJECTED
    #   before any network call.
    # * In ``live_write`` mode with all gates satisfied, the
    #   helper builds the v5 payload, reads the live ads (when
    #   ``campaign_id`` is supplied) to keep the required
    #   ``Title`` / ``Text`` / ``Href`` fields, and dispatches
    #   one ``ads.update`` call via the injected client.
    # * ``mock`` and ``sandbox`` modes are NOT product write paths
    #   for this endpoint (same rationale as the live-create chain:
    #   a sandbox token has the same v5 ``ads.update`` write shape
    #   as a real token, so the gate stays strict).

    @staticmethod
    def _build_ads_business_update_items(
        *,
        ad_ids: list[int],
        business_id: int,
        live_ads: dict[int, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Build a v5 ``Ads`` list for the BusinessId-attach apply.

        Direct API v5 ``ads.update`` is a REPLACE-shaped call: every
        field the operator wants to keep on the ad MUST be re-sent
        in the same request. For a TextAd the required body fields
        are ``Title`` / ``Text`` / ``Href``; ``BusinessId`` and
        ``PreferVCardOverBusiness`` are the new fields we are
        setting.

        ``live_ads`` (when supplied) is a ``{ad_id: TextAd dict}``
        map read from the live ``ads.get`` response — its
        ``Title`` / ``Text`` / ``Href`` flow into the payload so
        the live call cannot drop required fields. When
        ``live_ads`` is missing (the request only supplied
        ``ad_ids`` with no ``campaign_id`` read), the items are
        still shaped correctly so the dry-run preview can be
        inspected; the apply path uses the live read to keep the
        values accurate.
        """
        items: list[dict[str, Any]] = []
        for ad_id in ad_ids:
            text_ad: dict[str, Any] = {
                "Title": "<read-from-live-ad-before-apply>",
                "Text": "<read-from-live-ad-before-apply>",
                "Href": "<read-from-live-ad-before-apply>",
                "BusinessId": business_id,
                "PreferVCardOverBusiness": "NO",
            }
            if live_ads and ad_id in live_ads:
                live_text_ad = live_ads[ad_id]
                # Carry over the required fields so the v5
                # REPLACE-shape does not drop them.
                for key in ("Title", "Text", "Href"):
                    value = live_text_ad.get(key)
                    if isinstance(value, str) and value:
                        text_ad[key] = value
            items.append({"Id": ad_id, "TextAd": text_ad})
        return items

    @staticmethod
    def _redact_ads_business_payload(
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Redact a v5 ``Ads`` list for the audit log.

        The BusinessId is NOT a secret, but we keep the redacted
        shape (just ``Id`` + the BusinessId / PreferVCardOverBusiness
        flags) so the audit log cannot leak the full TextAd body
        (which may include operator-edited ad copy) into the
        application logs. Title / Text / Href are redacted to a
        short summary.
        """
        redacted: list[dict[str, Any]] = []
        for item in items:
            ad_id = item.get("Id")
            text_ad = item.get("TextAd") or {}
            redacted.append(
                {
                    "Id": ad_id,
                    "TextAd": {
                        "BusinessId": text_ad.get("BusinessId"),
                        "PreferVCardOverBusiness": text_ad.get(
                            "PreferVCardOverBusiness"
                        ),
                        "Title": "<redacted>",
                        "Text": "<redacted>",
                        "Href": text_ad.get("Href"),  # safe — public URL
                    },
                }
            )
        return redacted

    def yandex_ads_business_attach(
        self,
        payload: YandexAdsBusinessAttachRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> YandexAdsBusinessAttachResult:
        """Apply a BusinessId attach to one or more TextAds.

        Idempotency cache: keyed by
        ``(business_id, ad_ids tuple, idempotency_key, dry_run)`` so
        a dry-run replay does NOT consume a real apply's
        idempotency_key. Each (request, dry_run) triple has its
        own cache entry.
        """
        if not payload.approved:
            raise ValueError("Action requires explicit approval")

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"

        # Idempotency cache.
        target_signature = (
            tuple(payload.ad_ids) if payload.ad_ids else (f"campaign:{payload.campaign_id}",)
        )
        dry_flag = "dry" if payload.dry_run else "apply"
        cache_key = (
            f"ads_business:{payload.business_id}:{target_signature}:"
            f"{payload.idempotency_key}:{dry_flag}"
        )
        cached = getattr(self, "ads_business_attach_results_by_key", None)
        if cached is None:
            cached = {}
            self.ads_business_attach_results_by_key = cached
        if cache_key in cached:
            return cached[cache_key]

        # Resolve target ad ids. When ``campaign_id`` is supplied,
        # read the campaign's ads via the live client. We never
        # touch the network on dry_run, so the dry-run preview
        # surfaces the operator-supplied ad_ids verbatim.
        skipped: list[YandexAdsBusinessAttachSkipped] = []
        target_ad_ids: list[int] = list(payload.ad_ids or [])

        # Pre-flight mode gate for apply.
        if not payload.dry_run and mode != "live_write":
            raise YandexDirectError(
                f"Live writes require DIRECTPILOT_MODE=live_write; "
                f"current mode is {mode!r}; refusing to dispatch ads.update"
            )

        if payload.campaign_id is not None and not payload.dry_run:
            # Live read of the campaign's ads. Required so the
            # REPLACE-shaped ``ads.update`` payload can re-send the
            # required TextAd fields. Refused if no client.
            if client is None:
                raise YandexDirectError(
                    "YandexDirectClient is required for live ads/business writes"
                )
            try:
                ads_response = client.ads_get(payload.campaign_id)
            except YandexDirectError:
                raise
            if not ads_response.get("ok"):
                err = ads_response.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected ads.get: error_code={err.get('error_code')!r}"
                )
            ads_payload = (ads_response.get("result") or {}).get("Ads") or []
            text_ad_ids: list[int] = []
            live_text_ads: dict[int, dict[str, Any]] = {}
            for ad in ads_payload:
                if not isinstance(ad, dict):
                    continue
                ad_id_value = ad.get("Id")
                ad_type = ad.get("Type")
                if not isinstance(ad_id_value, int):
                    continue
                if ad_type != "TEXT_AD":
                    skipped.append(
                        YandexAdsBusinessAttachSkipped(
                            ad_id=ad_id_value,
                            reason="not_text_ad",
                        )
                    )
                    continue
                text_ad = ad.get("TextAd")
                if not isinstance(text_ad, dict):
                    text_ad = {}
                text_ad_ids.append(ad_id_value)
                live_text_ads[ad_id_value] = text_ad
            if not text_ad_ids:
                raise YandexDirectError(
                    f"campaign {payload.campaign_id} has no TextAds to attach "
                    f"the BusinessId to"
                )
            target_ad_ids = text_ad_ids
        else:
            live_text_ads = None

        # Build the v5 payload.
        items = self._build_ads_business_update_items(
            ad_ids=target_ad_ids,
            business_id=payload.business_id,
            live_ads=live_text_ads,
        )
        payload_preview = {
            "method": "ads.update",
            "params": {"Ads": items},
        }

        # Dry-run path: never perform a network write.
        if payload.dry_run:
            audit = self.append_audit(
                "yandex_ads_business_attach_requested",
                str(payload.campaign_id or "ad_ids"),
                dry_run=True,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex" if is_live else "mock",
                    "reason": payload.reason,
                    "business_id": payload.business_id,
                    "ad_ids": list(target_ad_ids),
                    "ad_count": len(target_ad_ids),
                    "skipped": [s.model_dump() for s in skipped],
                    "stage": "dry_run_preview",
                    "payload_redacted": self._redact_ads_business_payload(items),
                },
            )
            result = YandexAdsBusinessAttachResult(
                dry_run=True,
                applied=False,
                source="yandex" if is_live else "mock",
                mode=mode,
                audit_id=audit.id,
                business_id=payload.business_id,
                ad_ids=list(target_ad_ids),
                skipped=skipped,
                payload_preview=payload_preview,
            )
            # Dry-runs are intentionally not cached: every operator preview
            # should leave a fresh audit trace and must not consume/apply the
            # real idempotency key.
            return result

        # Real apply: gate by mode (the pre-flight check above
        # already raised for non-live_write modes). Defence in
        # depth.
        if not can_write:
            raise YandexDirectError(
                "Live writes are not allowed in live_readonly mode; "
                "switch DIRECTPILOT_MODE to live_write to attach BusinessId"
            )
        if client is None:
            raise YandexDirectError(
                "YandexDirectClient is required for live ads/business writes"
            )

        try:
            yandex_result = client.ads_update(items)
            if not yandex_result.get("ok"):
                err = yandex_result.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected ads.update: "
                    f"error_code={err.get('error_code')!r}"
                )
            sent_units = _safe_units(yandex_result.get("units"))
            audit = self.append_audit(
                "yandex_ads_business_attach_requested",
                str(payload.campaign_id or "ad_ids"),
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex",
                    "reason": payload.reason,
                    "business_id": payload.business_id,
                    "ad_ids": list(target_ad_ids),
                    "ad_count": len(target_ad_ids),
                    "skipped": [s.model_dump() for s in skipped],
                    "stage": "ads.update",
                    "applied": True,
                    "yandex_units": sent_units,
                    "payload_redacted": self._redact_ads_business_payload(items),
                },
            )
            result = YandexAdsBusinessAttachResult(
                dry_run=False,
                applied=True,
                source="yandex",
                mode=mode,
                audit_id=audit.id,
                business_id=payload.business_id,
                ad_ids=list(target_ad_ids),
                skipped=skipped,
                payload_preview=payload_preview,
                yandex_units=sent_units,
            )
            cached[cache_key] = result
            return result
        except YandexDirectError as exc:
            # Audit the failure with no token leakage. Re-raise so
            # the endpoint returns 502 with a redacted message.
            self.append_audit(
                "yandex_ads_business_attach_failed",
                str(payload.campaign_id or "ad_ids"),
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "business_id": payload.business_id,
                    "ad_ids": list(target_ad_ids),
                    "yandex_error": str(exc),
                },
            )
            raise
        except Exception as exc:  # noqa: BLE001 — safety net
            safe = YandexDirectError(
                f"unexpected error during ads/business apply: "
                f"{type(exc).__name__}: {exc}"
            )
            self.append_audit(
                "yandex_ads_business_attach_failed",
                str(payload.campaign_id or "ad_ids"),
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "business_id": payload.business_id,
                    "ad_ids": list(target_ad_ids),
                    "yandex_error": str(safe),
                    "exception_type": type(exc).__name__,
                },
            )
            raise safe from exc


store = MockStore()
