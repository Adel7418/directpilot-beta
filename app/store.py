from __future__ import annotations

import datetime as _dt
import hashlib
import json
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
    LiveAdCreateRequest,
    LiveAdCreateResult,
    LiveAdCreateItem,
    LiveAdCreateWarning,
    AdsModerateRequest,
    AdsModerateResult,
    ProviderWarning,
    YandexStrategyReadResult,
    YandexStrategyRequest,
    YandexStrategyResult,
    MULTI_GOAL_STRATEGY_ID,
    # Autotargeting
    AUTOTARGETING_CATEGORIES,
    AUTOTARGETING_BRAND_OPTIONS,
    YandexAutotargetingReadItem,
    YandexAutotargetingReadResult,
    YandexAutotargetingRequest,
    YandexAutotargetingResult,
    # Keyword bids
    KeywordBidItem,
    KeywordBidSetItemResult,
    KeywordBidTrafficLevelItem,
    KeywordBidTrafficLevelRequest,
    KeywordBidTrafficLevelResult,
    KeywordBidUpdateRequest,
    KeywordBidUpdateResult,
    AuctionForecastAuctionBid,
    AuctionForecastItem,
    AuctionForecastResult,
    # Bid modifiers
    BidModifierSetItemResult,
    BidModifiersUpdateRequest,
    BidModifiersUpdateResult,
    # UTM
    UtmAuditItem,
    UtmAuditResult,
    UtmApplyRequest,
    UtmApplyResult,
    UtmChangeItem,
    UtmConfig,
    UtmPlanRequest,
    UtmPlanResult,
)
from app.utm_builder import (
    DEFAULT_UTM_PARAMS,
    REQUIRED_UTM_PARAMS,
    audit_utm_url,
    build_utm_url,
    generate_campaign_slug,
)
from app.yandex_direct import YandexDirectClient, YandexDirectError
from app.yandex_facade import mock_yandex


_AUCTION_FORECAST_BATCH_SIZE = 200


def _normalize_phrase(value: str) -> str:
    return " ".join(value.split()).strip().lower()


def _keyword_bids_request_fingerprint(v5_items: list[dict[str, Any]]) -> str:
    """Build a canonical fingerprint for keyword bids payload for idempotency."""

    normalized = sorted(
        [dict(item) for item in v5_items],
        key=lambda item: (item.get("KeywordId"), json.dumps(item, sort_keys=True)),
    )
    canonical = json.dumps(
        {"method": "set", "params": {"KeywordBids": normalized}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bid_modifiers_request_fingerprint(payload: dict[str, Any]) -> str:
    """Build a canonical fingerprint for bid-modifier previews/applies."""

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _provider_warnings_from_result(
    yandex_result: dict[str, Any],
) -> list[ProviderWarning]:
    """Extract provider warnings from a ``_call`` result envelope.

    Returns a list of :class:`ProviderWarning` items, each with
    ``code``, ``message``, and ``details`` drawn from the v5
    ``Warnings[]`` array.  The raw envelope is never included —
    only the three redacted fields per warning.
    """
    raw = yandex_result.get("warnings")
    if not isinstance(raw, list):
        return []
    out: list[ProviderWarning] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = item.get("Code")
        message = item.get("Message")
        details = item.get("Details")
        if code is None and message is None and details is None:
            continue
        out.append(
            ProviderWarning(
                code=int(code) if code is not None else 0,
                message=str(message) if message is not None else "",
                details=str(details) if details is not None else "",
            )
        )
    return out


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


def _format_set_result_error(errors: list[Any]) -> str:
    """Render a v5 ``SetResults[].Errors[]`` array as a redacted one-line
    summary suitable for an audit event / HTTP detail.

    Same contract as ``_format_add_result_error`` but for ``SetResults``
    items (``keywordbids.set`` per-item errors).  Keeps only the short
    ``Code`` and a truncated ``Message`` — never includes raw payload.
    """
    if not errors:
        return "SetResults.Errors present but empty"
    parts: list[str] = []
    for err in errors[:3]:
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
    return "SetResults.Errors: " + "; ".join(parts)


def _set_result_keyword_id(item: dict[str, Any]) -> int | None:
    """Return a strictly valid keyword id from a KeywordBids.set item result.

    KeywordBids.set returns ``KeywordId`` for the item it processed.  Keep
    ``Id`` as a compatibility alias only when it is a positive integer and,
    when both fields are present, they agree.  Do not coerce missing,
    string, boolean, zero, negative, or conflicting values into a synthetic
    identifier.
    """
    identifiers: list[int] = []
    for field_name in ("KeywordId", "Id"):
        if field_name not in item:
            continue
        value = item[field_name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        identifiers.append(value)
    if not identifiers or len(set(identifiers)) != 1:
        return None
    return identifiers[0]


def _extract_set_results(
    result_payload: Any,
) -> tuple[list["KeywordBidSetItemResult"] | None, str | None]:
    """Extract and inspect per-item ``SetResults`` from a v5 ``keywordbids.set`` response.

    Returns ``(set_results, error_summary)`` where:
    * ``set_results`` — list of :class:`KeywordBidSetItemResult` with
      per-item outcomes (redacted errors/warnings).  ``None`` when the
      envelope is missing or malformed.
    * ``error_summary`` — ``None`` on success; a redacted one-line
      summary string when one or more items carry ``Errors`` (so the
      caller can surface it in ``yandex_error`` / audit).

    The raw v5 envelope is never included — only ``code``, ``message``,
    and ``details`` per warning/error.
    """
    if not isinstance(result_payload, dict):
        return None, None
    set_results_raw = result_payload.get("SetResults")
    if not isinstance(set_results_raw, list):
        # No SetResults to inspect — this happens on dry_run or
        # when the upstream call failed (ok=false).  Not an error here.
        return None, None

    item_results: list[KeywordBidSetItemResult] = []
    error_summaries: list[str] = []

    for item in set_results_raw:
        if not isinstance(item, dict):
            return None, "SetResults contains a non-object item"
        keyword_id = _set_result_keyword_id(item)
        if keyword_id is None:
            return None, "SetResults contains an invalid keyword identifier"

        item_errors_raw = item.get("Errors")
        item_warnings_raw = item.get("Warnings")

        item_errors: list[ProviderWarning] = []
        item_warnings: list[ProviderWarning] = []

        # Redact errors — only Code / Message / Details
        if isinstance(item_errors_raw, list) and item_errors_raw:
            for err in item_errors_raw:
                if isinstance(err, dict):
                    item_errors.append(
                        ProviderWarning(
                            code=int(err.get("Code") or 0),
                            message=str(err.get("Message") or ""),
                            details=str(err.get("Details") or ""),
                        )
                    )

        # Redact warnings — same contract
        if isinstance(item_warnings_raw, list):
            for warn in item_warnings_raw:
                if isinstance(warn, dict):
                    item_warnings.append(
                        ProviderWarning(
                            code=int(warn.get("Code") or 0),
                            message=str(warn.get("Message") or ""),
                            details=str(warn.get("Details") or ""),
                        )
                    )

        has_errors = len(item_errors) > 0
        has_warnings = len(item_warnings) > 0

        if has_errors:
            error_summaries.append(
                f"keyword_id={keyword_id}: "
                f"{_format_set_result_error(list(item_errors_raw))}"  # type: ignore[arg-type]
            )

        item_results.append(
            KeywordBidSetItemResult(
                keyword_id=keyword_id,
                has_errors=has_errors,
                has_warnings=has_warnings,
                errors=item_errors,
                warnings=item_warnings,
            )
        )

    error_summary: str | None = (
        "; ".join(error_summaries) if error_summaries else None
    )
    return item_results, error_summary


def _extract_bid_modifier_set_results(
    result_payload: Any,
) -> tuple[list["BidModifierSetItemResult"] | None, str | None]:
    """Extract redacted per-item outcomes from ``bidmodifiers.set``.

    Direct can return a top-level successful response while individual
    ``SetResults`` entries contain ``Errors``. Treat that as a partial failure
    so callers never see a false ``applied=True``.
    """
    if not isinstance(result_payload, dict):
        return None, None
    set_results_raw = result_payload.get("SetResults")
    if not isinstance(set_results_raw, list):
        return None, None

    item_results: list[BidModifierSetItemResult] = []
    error_summaries: list[str] = []

    for item in set_results_raw:
        if not isinstance(item, dict):
            continue
        modifier_id = item.get("Id") or 0
        item_errors_raw = item.get("Errors")
        item_warnings_raw = item.get("Warnings")

        item_errors: list[ProviderWarning] = []
        item_warnings: list[ProviderWarning] = []

        if isinstance(item_errors_raw, list) and item_errors_raw:
            for err in item_errors_raw:
                if isinstance(err, dict):
                    item_errors.append(
                        ProviderWarning(
                            code=int(err.get("Code") or 0),
                            message=str(err.get("Message") or ""),
                            details=str(err.get("Details") or ""),
                        )
                    )
        if isinstance(item_warnings_raw, list):
            for warn in item_warnings_raw:
                if isinstance(warn, dict):
                    item_warnings.append(
                        ProviderWarning(
                            code=int(warn.get("Code") or 0),
                            message=str(warn.get("Message") or ""),
                            details=str(warn.get("Details") or ""),
                        )
                    )

        has_errors = bool(item_errors)
        has_warnings = bool(item_warnings)
        if has_errors:
            error_summaries.append(
                f"modifier_id={modifier_id}: "
                f"{_format_set_result_error(list(item_errors_raw))}"  # type: ignore[arg-type]
            )

        item_results.append(
            BidModifierSetItemResult(
                modifier_id=int(modifier_id),
                has_errors=has_errors,
                has_warnings=has_warnings,
                errors=item_errors,
                warnings=item_warnings,
            )
        )

    error_summary = "; ".join(error_summaries) if error_summaries else None
    return item_results, error_summary


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
        # Strategy update results, keyed by (campaign_id, idempotency_key).
        # Mirrors time_targeting_results_by_key contract.
        self._strategy_results_by_key: dict[str, Any] = {}
        # Autotargeting update results, keyed by (campaign_id, idempotency_key).
        # Mirrors time_targeting_results_by_key / _strategy_results_by_key contract.
        self._autotargeting_results_by_key: dict[str, Any] = {}
        # Keyword bids update cache records keyed by (campaign_id, idempotency_key).
        # Stored value includes result + canonical request fingerprint for
        # payload-equality guard before replay.
        self._keyword_bids_results_by_key: dict[str, dict[str, Any]] = {}
        # Discrete auction traffic-level bid previews/applies have a separate
        # idempotency namespace because their material request includes both
        # keyword ids and the selected official traffic level.
        self._keyword_bid_traffic_level_results_by_key: dict[str, dict[str, Any]] = {}
        # Bid modifiers update cache mirrors keyword bids idempotency semantics.
        self._bid_modifiers_results_by_key: dict[str, dict[str, Any]] = {}
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
            utm_config=payload.utm_config,
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
        # Build UTM-augmented ad representations for preview.
        ads_with_utm: list[dict[str, Any]] = []
        for a in draft.ads:
            ad_dict = a.model_dump()
            utm_href = MockStore._resolve_utm_href(
                a.landing_url, draft=draft, ad_id=a.id
            )
            ad_dict["landing_url"] = utm_href
            ads_with_utm.append(ad_dict)
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
            "ads": ads_with_utm,
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
                        "LandingUrl": MockStore._resolve_utm_href(
                            a.landing_url, draft=draft, ad_id=a.id
                        ),
                        "DisplayLinkPath": a.display_link_path,
                    }
                    for a in draft.ads
                ],
            },
        }
        # Build PreviewPayload with UTM-augmented ads so the operator
        # sees the landing_url that will actually be sent to Yandex.
        from app.models import Ad as AdModel
        preview_ads: list[Any] = []
        for a in draft.ads:
            utm_href = MockStore._resolve_utm_href(
                a.landing_url, draft=draft, ad_id=a.id
            )
            preview_ads.append(
                AdModel(
                    id=a.id,
                    ad_group_id=a.ad_group_id,
                    title=a.title,
                    text=a.text,
                    landing_url=utm_href,
                    display_link_path=a.display_link_path,
                )
            )
        return PreviewPayload(
            draft_id=draft_id,
            name=draft.name or f"{draft.business_type} {draft.region}",
            business_type=draft.business_type,
            region=draft.region,
            landing_url=draft.landing_url,
            budget=draft.budget,
            bids=draft.bids,
            ad_groups=draft.ad_groups,
            ads=preview_ads,
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

    # --------------------------------------------------------- UTM audit / plan / apply

    @staticmethod
    def _resolve_campaign_name(campaign_id: str, *, live: bool, client) -> str:
        """Resolve a campaign name for slug generation.

        In mock mode, uses the mock catalog. In live modes, attempts
        a campaigns.get call — falls back to ``"campaign-{id}"`` on
        any error (the slug remains unique via the id).
        """
        if not live or client is None:
            # Mock mode: name from mock catalog.
            for c in mock_yandex.list_campaigns():
                if c["id"] == campaign_id:
                    return c["name"]
            return ""
        try:
            resp = client.campaigns_get()
            if resp.get("ok"):
                result = resp.get("result") or {}
                for c in result.get("Campaigns") or []:
                    if str(c.get("Id")) == str(campaign_id):
                        return str(c.get("Name") or "")
        except Exception:
            pass
        return ""

    def utm_audit(
        self,
        campaign_id: str,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> UtmAuditResult:
        """Read-only UTM audit for all ad and sitelink URLs in a campaign."""
        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")

        ads_raw: list[dict[str, Any]] = []
        sitelinks_raw: list[dict[str, Any]] = []

        if is_live and client is not None:
            try:
                ads_resp = client.ads_get_detailed(campaign_id)
                if ads_resp.get("ok"):
                    ads_raw = (ads_resp.get("result") or {}).get("Ads") or []
            except YandexDirectError:
                pass
            # Collect sitelink set ids.
            sl_set_ids: set[int] = set()
            for a in ads_raw:
                if isinstance(a, dict):
                    ta = a.get("TextAd") or {}
                    sid = ta.get("SitelinkSetId")
                    if isinstance(sid, int):
                        sl_set_ids.add(sid)
            if sl_set_ids:
                try:
                    sl_resp = client.sitelinks_get(ids=sorted(sl_set_ids))
                    if sl_resp.get("ok"):
                        sl_result = sl_resp.get("result") or {}
                        sitelinks_raw = sl_result.get("SitelinksSets") or []
                except YandexDirectError:
                    pass
        else:
            # Mock mode: use mock catalog.
            ads_raw = mock_yandex.list_ads_detailed(campaign_id)
            sl_set_ids: set[int] = set()
            for a in ads_raw:
                ta = a.get("TextAd") or {}
                sid = ta.get("SitelinkSetId")
                if isinstance(sid, int):
                    sl_set_ids.add(sid)
            sitelinks_raw = mock_yandex.list_sitelinks(sorted(sl_set_ids))

        items: list[UtmAuditItem] = []
        warnings: list[str] = []

        # Audit ad URLs.
        for a in ads_raw:
            if not isinstance(a, dict):
                continue
            ta = a.get("TextAd") or {}
            href = ta.get("Href", "")
            if not href or not isinstance(href, str) or not href.strip():
                continue
            ad_id = str(a.get("Id", ""))
            audit = audit_utm_url(
                href,
                required_params=REQUIRED_UTM_PARAMS,
                expected_values=DEFAULT_UTM_PARAMS,
            )
            items.append(
                UtmAuditItem(
                    entity_type="ad",
                    entity_id=ad_id,
                    url=href,
                    utm_status=audit["status"],
                    present_params=audit["present_params"],
                    missing_params=audit["missing_params"],
                    wrong_values=audit["wrong_values"],
                )
            )

        # Audit sitelink URLs.
        sitelink_items_count = 0
        for sl_set in sitelinks_raw:
            if not isinstance(sl_set, dict):
                continue
            for sl in sl_set.get("Sitelinks") or []:
                if not isinstance(sl, dict):
                    continue
                href = sl.get("Href")
                if not href or not isinstance(href, str) or not href.strip():
                    continue
                sl_title = str(sl.get("Title", ""))
                audit = audit_utm_url(
                    href,
                    required_params=REQUIRED_UTM_PARAMS,
                    expected_values=DEFAULT_UTM_PARAMS,
                )
                items.append(
                    UtmAuditItem(
                        entity_type="sitelink",
                        entity_id=f"{sl_set.get('Id', '?')}/{sl_title}",
                        url=href,
                        utm_status=audit["status"],
                        present_params=audit["present_params"],
                        missing_params=audit["missing_params"],
                        wrong_values=audit["wrong_values"],
                    )
                )
                sitelink_items_count += 1

        # Count stats.
        complete = sum(1 for i in items if i.utm_status == "complete")
        partial = sum(1 for i in items if i.utm_status == "partial")
        missing = sum(1 for i in items if i.utm_status == "missing")
        mismatch = sum(1 for i in items if i.utm_status == "mismatch")
        ads_count = sum(1 for i in items if i.entity_type == "ad")

        return UtmAuditResult(
            campaign_id=campaign_id,
            source="yandex" if is_live else "mock",
            read_only=True,
            ads_total=ads_count,
            sitelinks_total=sitelink_items_count,
            complete_count=complete,
            partial_count=partial,
            missing_count=missing,
            mismatch_count=mismatch,
            items=items,
            warnings=warnings,
        )

    def utm_plan(
        self,
        campaign_id: str,
        payload: UtmPlanRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> UtmPlanResult:
        """Generate UTM plan/preview — always dry_run, never writes."""
        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")

        ads_raw: list[dict[str, Any]] = []
        sitelinks_raw: list[dict[str, Any]] = []

        # Resolve campaign slug.
        campaign_name = self._resolve_campaign_name(campaign_id, live=is_live, client=client)
        campaign_slug = payload.campaign_slug or generate_campaign_slug(campaign_name, campaign_id)

        if is_live and client is not None:
            try:
                ads_resp = client.ads_get_detailed(campaign_id)
                if ads_resp.get("ok"):
                    ads_raw = (ads_resp.get("result") or {}).get("Ads") or []
            except YandexDirectError:
                pass
            sl_set_ids: set[int] = set()
            for a in ads_raw:
                if isinstance(a, dict):
                    ta = a.get("TextAd") or {}
                    sid = ta.get("SitelinkSetId")
                    if isinstance(sid, int):
                        sl_set_ids.add(sid)
            if sl_set_ids and payload.include_sitelinks:
                try:
                    sl_resp = client.sitelinks_get(ids=sorted(sl_set_ids))
                    if sl_resp.get("ok"):
                        sl_result = sl_resp.get("result") or {}
                        sitelinks_raw = sl_result.get("SitelinksSets") or []
                except YandexDirectError:
                    pass
        else:
            ads_raw = mock_yandex.list_ads_detailed(campaign_id)
            sl_set_ids: set[int] = set()
            for a in ads_raw:
                ta = a.get("TextAd") or {}
                sid = ta.get("SitelinkSetId")
                if isinstance(sid, int):
                    sl_set_ids.add(sid)
            if payload.include_sitelinks:
                sitelinks_raw = mock_yandex.list_sitelinks(sorted(sl_set_ids))

        items: list[UtmChangeItem] = []
        sitelink_items: list[UtmChangeItem] = []
        warnings: list[str] = []
        not_implemented: list[str] = []

        # Build ad URL changes.
        v5_ads: list[dict[str, Any]] = []
        for a in ads_raw:
            if not isinstance(a, dict):
                continue
            ta = a.get("TextAd") or {}
            href = ta.get("Href", "")
            if not href or not isinstance(href, str) or not href.strip():
                continue
            ad_id = a.get("Id")
            # Audit current URL.
            audit = audit_utm_url(
                href,
                required_params=REQUIRED_UTM_PARAMS,
                expected_values=DEFAULT_UTM_PARAMS,
            )
            # If UTM already complete and overwrite=False, skip.
            if audit["status"] == "complete" and not payload.overwrite:
                warnings.append(
                    f"Ad {ad_id}: UTM already complete, skipped (overwrite=False). "
                    f"Set overwrite=True to replace."
                )
                continue

            new_url = build_utm_url(
                href,
                utm_source="yandex",
                utm_medium="cpc",
                utm_campaign=campaign_slug,
                utm_content=str(ad_id),
                utm_term="",
                overwrite=payload.overwrite,
                custom_params=payload.custom_params,
            )
            items.append(
                UtmChangeItem(
                    entity_type="ad",
                    entity_id=str(ad_id),
                    old_url=href,
                    new_url=new_url,
                    utm_status_before=audit["status"],
                )
            )
            # Build v5 ads.update payload item (REPLACE-shaped).
            v5_ad = {
                "Id": ad_id,
                "TextAd": {
                    "Title": ta.get("Title", ""),
                    "Text": ta.get("Text", ""),
                    "Href": new_url,
                },
            }
            # Preserve optional fields.
            for field in ("SitelinkSetId", "BusinessId", "VCardId", "PreferVCardOverBusiness"):
                if field in ta and ta[field] is not None:
                    v5_ad["TextAd"][field] = ta[field]
            if "Title2" in ta and ta["Title2"] is not None:
                v5_ad["TextAd"]["Title2"] = ta["Title2"]
            v5_ads.append(v5_ad)

        # Sitelink URL changes.
        if payload.include_sitelinks:
            for sl_set in sitelinks_raw:
                if not isinstance(sl_set, dict):
                    continue
                sl_set_id = sl_set.get("Id", "?")
                for sl in sl_set.get("Sitelinks") or []:
                    if not isinstance(sl, dict):
                        continue
                    href = sl.get("Href")
                    if not href or not isinstance(href, str) or not href.strip():
                        continue
                    audit = audit_utm_url(
                        href,
                        required_params=REQUIRED_UTM_PARAMS,
                        expected_values=DEFAULT_UTM_PARAMS,
                    )
                    if audit["status"] == "complete" and not payload.overwrite:
                        warnings.append(
                            f"Sitelink {sl_set_id}/{sl.get('Title', '?')}: UTM already complete, "
                            "skipped (overwrite=False)."
                        )
                        continue
                    new_url = build_utm_url(
                        href,
                        utm_source="yandex",
                        utm_medium="cpc",
                        utm_campaign=campaign_slug,
                        utm_content="",
                        utm_term="",
                        overwrite=payload.overwrite,
                        custom_params=payload.custom_params,
                    )
                    sitelink_items.append(
                        UtmChangeItem(
                            entity_type="sitelink",
                            entity_id=f"{sl_set_id}/{sl.get('Title', '?')}",
                            old_url=href,
                            new_url=new_url,
                            utm_status_before=audit["status"],
                        )
                    )

        payload_preview = {
            "method": "ads.update",
            "params": {"Ads": v5_ads},
        } if v5_ads else None

        return UtmPlanResult(
            campaign_id=campaign_id,
            source="yandex" if is_live else "mock",
            dry_run=True,
            applied=False,
            campaign_slug=campaign_slug,
            items=items,
            sitelink_items=sitelink_items,
            payload_preview=payload_preview,
            warnings=warnings,
            not_implemented=not_implemented,
        )

    def utm_apply(
        self,
        campaign_id: str,
        payload: UtmApplyRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> UtmApplyResult:
        """Apply UTM URLs to live ads and optional sitelinks — write-gated.

        Gate contract: dry_run=True → preview only. dry_run=False
        requires live_write + approved + idempotency_key.
        Sitelink URLs are updated through ``sitelinks.update`` when requested.
        """
        if not payload.approved:
            raise ValueError("Action requires explicit approval")

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"

        # Idempotency cache.
        cache_key = f"utm_apply:{campaign_id}:{payload.idempotency_key}:{'dry' if payload.dry_run else 'apply'}"
        cached = getattr(self, "utm_apply_results_by_key", None)
        if cached is None:
            cached = {}
            self.utm_apply_results_by_key = cached
        if cache_key in cached:
            return cached[cache_key]

        # Pre-flight mode gate for real apply.
        if not payload.dry_run and not can_write:
            raise YandexDirectError(
                f"Live writes require DIRECTPILOT_MODE=live_write; "
                f"current mode is {mode!r}; refusing to dispatch ads.update for UTM"
            )

        # Resolve campaign slug.
        campaign_name = self._resolve_campaign_name(campaign_id, live=is_live, client=client)
        campaign_slug = payload.campaign_slug or generate_campaign_slug(campaign_name, campaign_id)

        # Read ads with full TextAd fields.
        ads_raw: list[dict[str, Any]] = []
        if is_live and client is not None:
            try:
                ads_resp = client.ads_get_detailed(campaign_id)
                if ads_resp.get("ok"):
                    ads_raw = (ads_resp.get("result") or {}).get("Ads") or []
            except YandexDirectError as exc:
                raise YandexDirectError(
                    f"Failed to read ads for UTM apply: {exc}"
                ) from exc
        else:
            ads_raw = mock_yandex.list_ads_detailed(campaign_id)

        # Build v5 ads.update payload.
        v5_ads: list[dict[str, Any]] = []
        target_ad_ids: list[int] = []
        expected_ad_readback_hrefs: dict[int, str] = {}
        warnings: list[str] = []
        not_implemented: list[str] = []
        sitelink_items: list[UtmChangeItem] = []

        for a in ads_raw:
            if not isinstance(a, dict):
                continue
            ta = a.get("TextAd") or {}
            href = ta.get("Href", "")
            if not href or not isinstance(href, str) or not href.strip():
                continue
            ad_id = a.get("Id")
            if not isinstance(ad_id, int):
                continue
            audit = audit_utm_url(
                href,
                required_params=REQUIRED_UTM_PARAMS,
                expected_values=DEFAULT_UTM_PARAMS,
            )
            if audit["status"] == "complete" and not payload.overwrite:
                warnings.append(
                    f"Ad {ad_id}: UTM already complete, skipped (overwrite=False)."
                )
                continue

            new_url = build_utm_url(
                href,
                utm_source="yandex",
                utm_medium="cpc",
                utm_campaign=campaign_slug,
                utm_content=str(ad_id),
                utm_term="",
                overwrite=payload.overwrite,
                custom_params=payload.custom_params,
            )

            v5_ad: dict[str, Any] = {
                "Id": ad_id,
                "TextAd": {
                    "Title": ta.get("Title", ""),
                    "Text": ta.get("Text", ""),
                    "Href": new_url,
                },
            }
            for field in ("SitelinkSetId", "BusinessId", "VCardId", "PreferVCardOverBusiness"):
                if field in ta and ta[field] is not None:
                    v5_ad["TextAd"][field] = ta[field]
            if "Title2" in ta and ta["Title2"] is not None:
                v5_ad["TextAd"]["Title2"] = ta["Title2"]
            v5_ads.append(v5_ad)
            target_ad_ids.append(ad_id)
            expected_ad_readback_hrefs[ad_id] = new_url

        # Sitelinks: optional preview + optional apply when include_sitelinks=True.
        sitelink_payloads: list[dict[str, Any]] = []
        expected_sitelink_readback: dict[int, list[dict[str, str]]] = {}
        if payload.include_sitelinks:
            sl_set_ids: set[int] = set()
            for a in ads_raw:
                if not isinstance(a, dict):
                    continue
                ta = a.get("TextAd") or {}
                sid = ta.get("SitelinkSetId")
                if isinstance(sid, int):
                    sl_set_ids.add(sid)
            if sl_set_ids:
                sitelinks_raw: list[dict[str, Any]] = []
                if is_live and client is not None:
                    try:
                        sl_resp = client.sitelinks_get(ids=sorted(sl_set_ids))
                        if sl_resp.get("ok"):
                            sitelinks_raw = (sl_resp.get("result") or {}).get("SitelinksSets") or []
                    except YandexDirectError:
                        pass
                else:
                    sitelinks_raw = mock_yandex.list_sitelinks(sorted(sl_set_ids))

                for sl_set in sitelinks_raw:
                    if not isinstance(sl_set, dict):
                        continue
                    sl_set_id = sl_set.get("Id")
                    if not isinstance(sl_set_id, int):
                        continue
                    full_items: list[dict[str, Any]] = []
                    set_has_changes = False
                    for sl in sl_set.get("Sitelinks") or []:
                        if not isinstance(sl, dict):
                            continue
                        item = dict(sl)
                        href = sl.get("Href")
                        if not href or not isinstance(href, str) or not href.strip():
                            full_items.append(item)
                            continue
                        audit = audit_utm_url(
                            href,
                            required_params=REQUIRED_UTM_PARAMS,
                            expected_values=DEFAULT_UTM_PARAMS,
                        )
                        if audit["status"] == "complete" and not payload.overwrite:
                            warnings.append(
                                f"Sitelink {sl_set_id}/{sl.get('Title', '?')}: UTM already complete, "
                                "skipped (overwrite=False)."
                            )
                            full_items.append(item)
                            continue
                        new_sl_url = build_utm_url(
                            href,
                            utm_source="yandex",
                            utm_medium="cpc",
                            utm_campaign=campaign_slug,
                            utm_content="",
                            utm_term="",
                            overwrite=payload.overwrite,
                            custom_params=payload.custom_params,
                        )
                        item["Href"] = new_sl_url
                        set_has_changes = True
                        sitelink_items.append(
                            UtmChangeItem(
                                entity_type="sitelink",
                                entity_id=f"{sl_set_id}/{sl.get('Title', '?')}",
                                old_url=href,
                                new_url=new_sl_url,
                                utm_status_before=audit["status"],
                            )
                        )
                        expected_sitelink_readback.setdefault(sl_set_id, []).append(
                            {
                                "title": str(sl.get("Title", "")),
                                "href": new_sl_url,
                            }
                        )
                        full_items.append(item)
                    if set_has_changes:
                        missing_fields = [f for f in ("Name", "Status", "Type") if sl_set.get(f) is None]
                        if missing_fields:
                            if is_live:
                                raise ValueError(
                                    f"Cannot update sitelinks set {sl_set_id} for UTM: "
                                    f"missing required fields {missing_fields!r} from Yandex API get response"
                                )
                            fallback_values = {"Name": "Primary sitelinks", "Status": "ACTIVE", "Type": "TEXT"}
                            sl_set = dict(sl_set)
                            for field in missing_fields:
                                sl_set[field] = fallback_values[field]
                        set_payload = dict(sl_set)
                        set_payload["Sitelinks"] = full_items
                        sitelink_payloads.append(set_payload)

        payload_preview = {
            "method": "ads.update",
            "params": {"Ads": v5_ads},
        } if v5_ads else None

        if payload.include_sitelinks and sitelink_payloads:
            sl_payload_preview = {"method": "sitelinks.update", "params": {"SitelinksSets": sitelink_payloads}}
            if payload_preview is None:
                payload_preview = {
                    "method": "ads.update",
                    "params": {"Ads": []},
                }
            payload_preview = {
                **payload_preview,
                "sitelinks_preview": sl_payload_preview,
            }

        # Dry-run path: never call ads.update.
        if payload.dry_run:
            audit = self.append_audit(
                "utm_apply_requested",
                campaign_id,
                dry_run=True,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex" if is_live else "mock",
                    "campaign_slug": campaign_slug,
                    "ad_ids": target_ad_ids,
                    "ad_count": len(target_ad_ids),
                    "overwrite": payload.overwrite,
                    "stage": "dry_run_preview",
                },
            )
            result = UtmApplyResult(
                campaign_id=campaign_id,
                source="yandex" if is_live else "mock",
                mode=mode,
                dry_run=True,
                applied=False,
                audit_id=audit.id,
                campaign_slug=campaign_slug,
                ad_ids=[],
                sitelink_items=sitelink_items,
                payload_preview=payload_preview,
                warnings=warnings,
                not_implemented=not_implemented,
            )
            return result

        # Real apply: gate already validated.
        if client is None:
            raise YandexDirectError(
                "YandexDirectClient is required for live UTM apply writes"
            )

        if not v5_ads and not sitelink_payloads:
            raise YandexDirectError(
                "No ads or sitelinks with URLs to update — all URLs either are missing "
                "or already have complete UTM with overwrite=False"
            )

        try:
            sent_units: int | None = None
            provider_warnings: list[ProviderWarning] = []
            if v5_ads:
                yandex_result = client.ads_update(v5_ads)
                if not yandex_result.get("ok"):
                    err = yandex_result.get("error") or {}
                    raise YandexDirectError(
                        f"Yandex Direct rejected ads.update for UTM: "
                        f"error_code={err.get('error_code')!r}"
                    )
                sent_units = _safe_units(yandex_result.get("units"))
                provider_warnings = _provider_warnings_from_result(yandex_result)

            if payload.include_sitelinks and sitelink_payloads:
                sitelinks_result = client.sitelinks_update(sitelink_payloads)
                if not sitelinks_result.get("ok"):
                    err = sitelinks_result.get("error") or {}
                    raise YandexDirectError(
                        f"Yandex Direct rejected sitelinks.update for UTM: "
                        f"error_code={err.get('error_code')!r}"
                    )
                provider_warnings.extend(_provider_warnings_from_result(sitelinks_result))
                sl_units = _safe_units(sitelinks_result.get("units"))
                if sl_units is not None:
                    if sent_units is None:
                        sent_units = sl_units
                    else:
                        sent_units += sl_units

            # Readback: confirm new URLs.
            readback: list[dict[str, Any]] | None = None
            sitelink_readback: list[dict[str, Any]] | None = None
            if target_ad_ids:
                rb_resp = client.ads_get_by_ids(target_ad_ids)
                if not rb_resp.get("ok"):
                    err = rb_resp.get("error") or {}
                    raise YandexDirectError(
                        f"Yandex Direct readback failed after ads.update for UTM: "
                        f"error_code={err.get('error_code')!r}"
                    )
                rb_ads = (rb_resp.get("result") or {}).get("Ads") or []
                rb_ad_map: dict[int, str] = {}
                for a in rb_ads:
                    if not isinstance(a, dict):
                        continue
                    rb_ad_id = a.get("Id")
                    if isinstance(rb_ad_id, int):
                        rb_ad_map[rb_ad_id] = (a.get("TextAd") or {}).get("Href", "")
                readback = [
                    {
                        "Id": a.get("Id"),
                        "Href": (a.get("TextAd") or {}).get("Href", ""),
                    }
                    for a in rb_ads
                    if isinstance(a, dict)
                ]
                if len(readback) < len(target_ad_ids):
                    raise YandexDirectError("Yandex Direct ads readback incomplete after UTM apply")
                missing_ad_ids = [
                    ad_id for ad_id in target_ad_ids if ad_id not in rb_ad_map
                ]
                if missing_ad_ids:
                    raise YandexDirectError(
                        "Yandex Direct ads readback incomplete after UTM apply: "
                        f"missing_ad_ids={missing_ad_ids!r}"
                    )
                mismatched_ad_urls = [
                    {
                        "ad_id": ad_id,
                        "expected_href": expected_href,
                        "actual_href": rb_ad_map.get(ad_id, ""),
                    }
                    for ad_id, expected_href in expected_ad_readback_hrefs.items()
                    if rb_ad_map.get(ad_id) != expected_href
                ]
                if mismatched_ad_urls:
                    raise YandexDirectError(
                        "Yandex Direct ads readback mismatch after UTM apply: "
                        f"{mismatched_ad_urls!r}"
                    )
            if payload.include_sitelinks and sitelink_payloads:
                rb_sitelink_ids = sorted(expected_sitelink_readback.keys())
                if rb_sitelink_ids:
                    rb_sl_resp = client.sitelinks_get(ids=rb_sitelink_ids)
                    if not rb_sl_resp.get("ok"):
                        err = rb_sl_resp.get("error") or {}
                        raise YandexDirectError(
                            f"Yandex Direct readback failed after sitelinks.update for UTM: "
                            f"error_code={err.get('error_code')!r}"
                        )
                    rb_sets = (rb_sl_resp.get("result") or {}).get("SitelinksSets") or []
                    returned_set_ids: set[int] = set()
                    rb_set_map: dict[int, list[dict[str, Any]]] = {}
                    for rb_set in rb_sets:
                        if isinstance(rb_set, dict):
                            rb_set_id = rb_set.get("Id")
                            if isinstance(rb_set_id, int):
                                returned_set_ids.add(rb_set_id)
                                rb_set_map[rb_set_id] = []
                            for s in (rb_set.get("Sitelinks") or []):
                                if isinstance(s, dict):
                                    sitelink_readback = sitelink_readback or []
                                    sitelink_readback.append(
                                        {
                                            "sitelink_set_id": rb_set.get("Id"),
                                            "title": s.get("Title"),
                                            "href": s.get("Href", ""),
                                        }
                                    )
                                    if isinstance(rb_set_id, int):
                                        rb_set_map[rb_set_id].append(s)
                    expected_set_ids = set(rb_sitelink_ids)
                    if returned_set_ids != expected_set_ids:
                        missing = sorted(expected_set_ids - returned_set_ids)
                        raise YandexDirectError(
                            f"Yandex Direct sitelink readback incomplete after UTM apply: "
                            f"missing_set_ids={missing!r}"
                        )
                    for set_id, expected_links in expected_sitelink_readback.items():
                        rb_links = rb_set_map.get(set_id, [])
                        remaining: list[dict[str, Any]] = list(rb_links)
                        missing_links: list[tuple[str, str]] = []
                        for expected in expected_links:
                            exp_title = expected["title"]
                            exp_href = expected["href"]
                            match_index = next(
                                (
                                    idx
                                    for idx, rb_link in enumerate(remaining)
                                    if str(rb_link.get("Title", "")) == exp_title
                                    and str(rb_link.get("Href", "")) == exp_href
                                ),
                                None,
                            )
                            if match_index is None:
                                missing_links.append((exp_title, exp_href))
                            else:
                                remaining.pop(match_index)
                        if missing_links:
                            raise YandexDirectError(
                                "Yandex Direct sitelink readback mismatch after UTM apply: "
                                f"set_id={set_id}, missing_links={missing_links!r}"
                            )
                    if not sitelink_readback:
                        raise YandexDirectError("Yandex Direct sitelink readback empty after UTM apply")

            audit = self.append_audit(
                "utm_apply_requested",
                campaign_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex",
                    "campaign_slug": campaign_slug,
                    "ad_ids": target_ad_ids,
                    "ad_count": len(target_ad_ids),
                    "overwrite": payload.overwrite,
                    "stage": "ads.update",
                    "applied": True,
                    "yandex_units": sent_units,
                },
            )
            result = UtmApplyResult(
                campaign_id=campaign_id,
                source="yandex",
                mode=mode,
                dry_run=False,
                applied=True,
                audit_id=audit.id,
                campaign_slug=campaign_slug,
                ad_ids=target_ad_ids,
                sitelink_items=sitelink_items,
                payload_preview=payload_preview,
                readback=readback,
                sitelink_readback=sitelink_readback,
                provider_warnings=provider_warnings,
                warnings=warnings,
                not_implemented=not_implemented,
                yandex_units=sent_units,
            )
            cached[cache_key] = result
            return result
        except YandexDirectError as exc:
            self.append_audit(
                "utm_apply_failed",
                campaign_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "campaign_slug": campaign_slug,
                    "ad_ids": target_ad_ids,
                    "yandex_error": str(exc),
                },
            )
            raise
        except Exception as exc:
            safe = YandexDirectError(
                f"unexpected error during UTM apply: {type(exc).__name__}: {exc}"
            )
            self.append_audit(
                "utm_apply_failed",
                campaign_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "campaign_slug": campaign_slug,
                    "ad_ids": target_ad_ids,
                    "yandex_error": str(safe),
                    "exception_type": type(exc).__name__,
                },
            )
            raise safe from exc

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
    ) -> dict[str, Any]:
        """Build the v5 ``TimeTargeting`` block from a canonical schedule.

        The result is a dictionary with ``Schedule.Items`` (array of
        strings ``"D,bid0,bid1,...,bid23"`` where D is 1-7 for
        Monday-Sunday), ``ConsiderWorkingWeekends`` (required), and
        ``HolidaysSchedule`` (required by the v5 contract).

        This mirrors the documented v5 ``campaigns.update`` format at
        https://yandex.com/dev/direct/doc/en/curl-campaigns-timetargeting
        """
        from app.models import WEEK_DAY_NAMES

        if len(schedule.days) != len(WEEK_DAY_NAMES):
            raise YandexDirectError(
                f"TimeTargeting schedule must have 7 days, got {len(schedule.days)}"
            )
        items: list[str] = []
        for index, day in enumerate(schedule.days):
            bid_values = [str(int(b)) for b in day.hours]
            items.append(f"{index + 1},{','.join(bid_values)}")
        return {
            "Schedule": {"Items": items},
            "ConsiderWorkingWeekends": "YES",
            "HolidaysSchedule": {
                "StartHour": 14,
                "EndHour": 24,
                "SuspendOnHolidays": "NO",
                "BidPercent": 100,
            },
        }

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

    # Read-side → write-side normalization for BiddingStrategy.
    #
    # Direct v5 ``campaigns.get`` returns extra read-only fields in
    # BiddingStrategy sub-objects that the ``campaigns.update``
    # write-side does not accept.  We strip known read-only fields
    # and return a clean write-side dict.  We never invent field
    # names — we only remove fields that the write-side contract
    # does not document.
    #
    # ``BudgetType`` was previously treated as read-only because the
    # public Yandex docs do not list it in the write-side schema for
    # WbMaximumConversionRate / WbMaximumClicks.  However the live
    # API returns it on GET and rejects updates without it
    # (error_code=8000), so we now pass it through unchanged on
    # deterministic read→write mapping.
    _STRATEGY_READ_ONLY_FIELDS: frozenset[str] = frozenset()

    @classmethod
    def _normalize_strategy_for_write(
        cls,
        strategy: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize a BiddingStrategy block from read-side to
        write-side shape by removing read-only fields.

        The ``Search`` and ``Network`` sub-objects carry strategy-
        type-specific params. Read-only fields like ``BudgetType``
        are stripped from each sub-object's nested strategy params
        (e.g. ``WbMaximumConversionRate``, ``AverageCpa``, etc.)
        so the write-side contract is clean.
        """
        result: dict[str, Any] = {}
        for key, value in strategy.items():
            if isinstance(value, dict):
                result[key] = cls._normalize_strategy_sub_object(value)
            else:
                result[key] = value
        return result

    @classmethod
    def _normalize_strategy_sub_object(
        cls,
        obj: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize a Search or Network strategy sub-object."""
        result: dict[str, Any] = {}
        for key, value in obj.items():
            if isinstance(value, dict):
                # Nested strategy param (e.g. WbMaximumConversionRate,
                # AverageCpa, etc.) — strip read-only fields.
                cleaned = {
                    k: v
                    for k, v in value.items()
                    if k not in cls._STRATEGY_READ_ONLY_FIELDS
                }
                result[key] = cleaned
            elif key in cls._STRATEGY_READ_ONLY_FIELDS:
                # Top-level read-only field in the sub-object.
                continue
            else:
                result[key] = value
        return result

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
        #
        # The check MUST happen before the DailyBudget read so that
        # replays do not trigger unnecessary network calls.
        cache_key = f"time_targeting:{campaign_id}:{payload.idempotency_key}"
        if cache_key in self.time_targeting_results_by_key:
            cached = self.time_targeting_results_by_key[cache_key]
            if cached.dry_run != payload.dry_run:
                raise YandexDirectError(
                    f"Idempotency key {payload.idempotency_key!r} was "
                    f"previously used with dry_run={cached.dry_run}; "
                    f"replay with dry_run={payload.dry_run} is not allowed"
                )
            return cached

        # Read the current ``DailyBudget`` block from the live
        # campaign (if available) so the ``campaigns.update``
        # payload includes ``DailyBudget.Mode``. Direct v5 rejects
        # updates without ``Mode`` when the campaign has a daily
        # budget (error_code=8000). The read uses ``campaigns.get``
        # with ``FieldNames=[Id, Name, DailyBudget]``.
        #
        # ``SpendMode`` (read-side) → ``Mode`` (write-side):
        # Direct returns ``SpendMode`` on ``campaigns.get`` but
        # expects ``Mode`` on ``campaigns.add`` / ``campaigns.update``.
        # We normalise here and document the mapping.
        #
        # For TEXT_CAMPAIGN smart-strategy campaigns (e.g.
        # WB_MAXIMUM_CONVERSION_RATE) where DailyBudget is null,
        # we ALSO read ``TextCampaign.BiddingStrategy`` via a
        # separate ``campaigns.get`` call with
        # ``TextCampaignFieldNames``. Direct v5 requires the
        # BiddingStrategy block when updating a smart-strategy
        # campaign; omitting it yields error_code=8000 even when
        # only TimeTargeting is being changed.
        resolved_daily_budget: dict[str, Any] | None = None
        daily_budget_read_ok = False
        resolved_strategy: dict[str, Any] | None = None
        strategy_read_ok = False
        campaign_type: str | None = None

        if is_live and client is not None:
            try:
                budget_response = client.campaigns_get_daily_budget(campaign_id)
                if budget_response.get("ok"):
                    budget_result = budget_response.get("result") or {}
                    if isinstance(budget_result, dict):
                        budget_campaigns = budget_result.get("Campaigns") or []
                        if budget_campaigns and isinstance(budget_campaigns[0], dict):
                            daily_budget_read_ok = True
                            if "DailyBudget" not in budget_campaigns[0]:
                                # Ambiguous envelope: the field was requested
                                # but omitted from the response. This is
                                # different from an explicit ``DailyBudget:
                                # null`` and must fail closed before apply.
                                daily_budget_read_ok = False
                                raw_budget = None
                            else:
                                raw_budget = budget_campaigns[0].get("DailyBudget")
                            if raw_budget is None:
                                # No daily budget is configured for this
                                # campaign (for example after switching to a
                                # weekly conversion strategy). There is no
                                # DailyBudget.Mode value to preserve.
                                resolved_daily_budget = None
                            elif isinstance(raw_budget, dict) and raw_budget:
                                # Normalise SpendMode → Mode.
                                # Direct's read returns ``SpendMode``;
                                # the write contract requires ``Mode``.
                                resolved_daily_budget = dict(raw_budget)
                                spend_mode = resolved_daily_budget.pop(
                                    "SpendMode", None
                                )
                                if "Mode" not in resolved_daily_budget and spend_mode:
                                    resolved_daily_budget["Mode"] = spend_mode
                                elif "Mode" not in resolved_daily_budget:
                                    # No SpendMode and no Mode — ambiguous.
                                    resolved_daily_budget = None
            except Exception:
                # Best-effort read: if the budget read fails, we
                # cannot safely build the update payload.
                resolved_daily_budget = None
                daily_budget_read_ok = False

            # Read campaign Type and TextCampaign.BiddingStrategy.
            # Required for TEXT_CAMPAIGN smart-strategy campaigns
            # where DailyBudget is null but Direct still requires
            # the strategy block in campaigns.update.
            if daily_budget_read_ok and resolved_daily_budget is None:
                try:
                    strategy_response = client.campaigns_get_strategy(
                        campaign_id
                    )
                    if strategy_response.get("ok"):
                        strategy_result = (
                            strategy_response.get("result") or {}
                        )
                        if isinstance(strategy_result, dict):
                            strategy_campaigns = (
                                strategy_result.get("Campaigns") or []
                            )
                            if strategy_campaigns and isinstance(
                                strategy_campaigns[0], dict
                            ):
                                camp = strategy_campaigns[0]
                                tc = camp.get("TextCampaign")
                                if (
                                    isinstance(tc, dict)
                                    and "BiddingStrategy" in tc
                                ):
                                    strategy_read_ok = True
                                    resolved_strategy = self._normalize_strategy_for_write(
                                        tc["BiddingStrategy"]
                                    )
                except Exception:
                    # Best-effort: if the strategy read fails,
                    # resolved_strategy stays None.
                    resolved_strategy = None
                    strategy_read_ok = False

        # Build the campaigns.update payload entry.
        campaign_entry: dict[str, Any] = {
            "Id": YandexDirectClient._direct_id(campaign_id),
            "TimeTargeting": v5_time_targeting,
        }
        if resolved_daily_budget is not None:
            campaign_entry["DailyBudget"] = resolved_daily_budget
        if resolved_strategy is not None:
            campaign_entry["TextCampaign"] = {
                "BiddingStrategy": resolved_strategy,
            }
        payload_preview: dict[str, Any] = {
            "method": "campaigns.update",
            "params": {
                "Campaigns": [campaign_entry]
            },
        }

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

        # Fail closed if the budget read itself failed or returned an
        # ambiguous envelope. A successful read with ``DailyBudget: null``
        # means the campaign has no daily budget (for example weekly
        # conversion strategy), so there is no Mode value to preserve.
        if is_live and not daily_budget_read_ok:
            raise YandexDirectError(
                "Could not read current DailyBudget from campaign "
                f"{campaign_id!r}; refusing to send campaigns.update "
                "without DailyBudget.Mode (would yield error_code=8000)"
            )

        # Fail closed if DailyBudget is null (smart-strategy campaign)
        # but the BiddingStrategy read returned ambiguous/missing data.
        # Never invent strategy values.
        if (
            is_live
            and resolved_daily_budget is None
            and not strategy_read_ok
        ):
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
                    "yandex_error": (
                        "Could not read TextCampaign.BiddingStrategy"
                    ),
                },
            )
            raise YandexDirectError(
                "Could not read TextCampaign.BiddingStrategy from "
                f"campaign {campaign_id!r} (DailyBudget=null, "
                "likely smart-strategy); refusing to send "
                "campaigns.update without BiddingStrategy "
                "(would yield error_code=8000)"
            )

        try:
            yandex_result = client.campaigns_update_time_targeting(
                campaign_id,
                v5_time_targeting,
                daily_budget=resolved_daily_budget,
                text_campaign=(
                    {"BiddingStrategy": resolved_strategy}
                    if resolved_strategy is not None
                    else None
                ),
            )
            if not yandex_result.get("ok"):
                err = yandex_result.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected campaigns.update: "
                    f"error_code={err.get('error_code')!r}",
                    diagnostics={
                        "error_code": err.get("error_code"),
                        "error_detail": err.get("error_detail"),
                        "payload_preview": payload_preview,
                    },
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
                        if isinstance(candidate, dict):
                            readback_block = {"TimeTargeting": dict(candidate)}
                        elif isinstance(candidate, list):
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
                    "yandex_error_detail": exc.diagnostics.get(
                        "error_detail"
                    ),
                    "payload_preview": exc.diagnostics.get(
                        "payload_preview"
                    ),
                },
            )
            raise

    # ------------------------------------------------- strategy read / update
    #
    # ``POST /yandex/campaigns/{campaign_id}/strategy`` updates the
    # ``TextCampaign.BiddingStrategy`` for an existing campaign via
    # v5 ``campaigns.update``. The gate contract is identical to the
    # time-targeting endpoint: ``dry_run=True`` is preview-only,
    # real apply requires ``live_write``, ``approved=True``,
    # ``idempotency_key``, and ``dry_run=False``.
    #
    # ``weekly_spend_limit`` and ``bid_ceiling`` are received in RUBLES
    # (public REST convention) and converted to Direct micros
    # (multiply by 1_000_000) before building the v5 payload.
    #
    # The ``Network`` strategy is preserved from readback when the
    # request omits the ``network`` field. When ``network="SERVING_OFF"``
    # is set explicitly, it is applied as requested. The endpoint never
    # silently turns networks ON.
    #
    # ``BudgetType`` is preserved from the readback strategy block
    # (e.g. ``WEEKLY_BUDGET`` for WbMaximumConversionRate). Direct
    # requires it on update; stripping it caused live error_code=8000.

    _MICROS_PER_RUBLE: int = 1_000_000

    def yandex_strategy_update(
        self,
        campaign_id: str,
        payload: "YandexStrategyRequest",
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> "YandexStrategyResult":
        """Apply a strategy update for an existing campaign.

        Supports single-goal (``goal_id``) and multi-goal modes
        (``goal_ids`` / ``priority_goals``).  Multi-goal sets
        ``WbMaximumConversionRate.GoalId=13`` and populates
        ``TextCampaign.PriorityGoals.Items``.
        """

        if not payload.approved:
            raise ValueError("Action requires explicit approval")

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"

        # --- Resolve goal mode --------------------------------------------------
        _DEFAULT_PRIORITY_VALUE_RUB = 1.0  # equal-weight convenience default

        single_goal_id: int | None = payload.goal_id
        priority_goals_items: list[dict[str, Any]] | None = None

        if payload.goal_ids is not None:
            # Equal-weight multi-goal: GoalId=13, equal value 1.0 RUB each
            priority_goals_items = [
                {
                    "GoalId": gid,
                    "Value": int(_DEFAULT_PRIORITY_VALUE_RUB * self._MICROS_PER_RUBLE),
                    "Operation": "SET",
                }
                for gid in payload.goal_ids
            ]
        elif payload.priority_goals is not None:
            # Explicit priority goals: GoalId=13, caller-provided RUB values
            priority_goals_items = [
                {
                    "GoalId": pg.goal_id,
                    "Value": int(
                        (pg.value if pg.value is not None else _DEFAULT_PRIORITY_VALUE_RUB)
                        * self._MICROS_PER_RUBLE
                    ),
                    "Operation": "SET",
                }
                for pg in payload.priority_goals
            ]

        resolved_goal_id: int = (
            single_goal_id if single_goal_id is not None else MULTI_GOAL_STRATEGY_ID
        )
        is_multi_goal = single_goal_id is None

        # Idempotency cache check (mirrors time-targeting).
        cache_key = f"strategy:{campaign_id}:{payload.idempotency_key}"
        if cache_key in self._strategy_results_by_key:
            cached = self._strategy_results_by_key[cache_key]
            if cached.dry_run != payload.dry_run:
                raise YandexDirectError(
                    f"Idempotency key {payload.idempotency_key!r} was "
                    f"previously used with dry_run={cached.dry_run}; "
                    f"replay with dry_run={payload.dry_run} is not allowed"
                )
            return cached

        # Read current campaign to preserve DailyBudget (if any),
        # BudgetType from existing strategy, and Network strategy
        # when the request does not explicitly set it.
        current_daily_budget: dict[str, Any] | None = None
        current_strategy: dict[str, Any] | None = None
        daily_budget_read_ok = False
        strategy_read_ok = False

        if is_live and client is not None:
            # Read current DailyBudget + strategy via the same
            # full-strategy method used by the GET endpoint.
            try:
                strat_response = client.campaigns_get_full_strategy(campaign_id)
                if strat_response.get("ok"):
                    strat_result = strat_response.get("result") or {}
                    if isinstance(strat_result, dict):
                        strat_campaigns = strat_result.get("Campaigns") or []
                        if strat_campaigns and isinstance(strat_campaigns[0], dict):
                            camp = strat_campaigns[0]

                            # DailyBudget
                            if "DailyBudget" in camp:
                                raw_budget = camp.get("DailyBudget")
                                daily_budget_read_ok = True
                                if raw_budget is None:
                                    current_daily_budget = None
                                elif isinstance(raw_budget, dict) and raw_budget:
                                    current_daily_budget = dict(raw_budget)
                                    spend_mode = current_daily_budget.pop(
                                        "SpendMode", None
                                    )
                                    if "Mode" not in current_daily_budget and spend_mode:
                                        current_daily_budget["Mode"] = spend_mode
                                    elif "Mode" not in current_daily_budget:
                                        current_daily_budget = None
                            else:
                                # Field requested but absent — fail closed
                                daily_budget_read_ok = False

                            # TextCampaign.BiddingStrategy
                            tc = camp.get("TextCampaign")
                            if isinstance(tc, dict) and "BiddingStrategy" in tc:
                                strategy_read_ok = True
                                current_strategy = self._normalize_strategy_for_write(
                                    tc["BiddingStrategy"]
                                )
            except Exception:
                current_daily_budget = None
                daily_budget_read_ok = False
                current_strategy = None
                strategy_read_ok = False

        # Build the Search strategy from request.
        # Values in RUBLES → convert to micros for Direct.
        search_wb: dict[str, Any] = {
            "GoalId": resolved_goal_id,
            "WeeklySpendLimit": int(
                payload.weekly_spend_limit * self._MICROS_PER_RUBLE
            ),
        }
        if payload.bid_ceiling is not None:
            search_wb["BidCeiling"] = int(
                payload.bid_ceiling * self._MICROS_PER_RUBLE
            )

        # Preserve BudgetType from readback if present.
        if current_strategy is not None:
            current_search = current_strategy.get("Search")
            if isinstance(current_search, dict):
                for sub_key, sub_val in current_search.items():
                    if isinstance(sub_val, dict) and "BudgetType" in sub_val:
                        search_wb["BudgetType"] = sub_val["BudgetType"]

        new_search: dict[str, Any] = {
            "BiddingStrategyType": payload.strategy_type,
            "WbMaximumConversionRate": search_wb,
        }

        # Network: preserve from readback or use explicit request value.
        if payload.network is not None:
            new_network: dict[str, Any] = {
                "BiddingStrategyType": payload.network,
            }
        elif current_strategy is not None:
            net = current_strategy.get("Network")
            if isinstance(net, dict):
                new_network = dict(net)
            else:
                new_network = {"BiddingStrategyType": "SERVING_OFF"}
        else:
            new_network = {"BiddingStrategyType": "SERVING_OFF"}

        new_strategy: dict[str, Any] = {
            "Search": new_search,
            "Network": new_network,
        }

        text_campaign_block: dict[str, Any] = {
            "BiddingStrategy": new_strategy,
        }

        # PriorityGoals: always explicit to avoid ambiguous Direct API
        # semantics.  Single-goal mode clears any previously-set
        # PriorityGoals with Items=[].  Multi-goal mode populates
        # Items with the resolved priority goals.
        text_campaign_block["PriorityGoals"] = {
            "Items": (
                list(priority_goals_items)
                if priority_goals_items is not None
                else []
            ),
        }

        # Build the campaigns.update payload entry.
        campaign_entry: dict[str, Any] = {
            "Id": YandexDirectClient._direct_id(campaign_id),
            "TextCampaign": text_campaign_block,
        }
        if current_daily_budget is not None:
            campaign_entry["DailyBudget"] = current_daily_budget
        payload_preview: dict[str, Any] = {
            "method": "campaigns.update",
            "params": {
                "Campaigns": [campaign_entry]
            },
        }

        strategy_applied: dict[str, Any] = {
            "search": {
                "type": payload.strategy_type,
                "goal_id": resolved_goal_id,
                "weekly_spend_limit_rub": payload.weekly_spend_limit,
                "weekly_spend_limit_micros": int(
                    payload.weekly_spend_limit * self._MICROS_PER_RUBLE
                ),
            },
            "network": {
                "type": new_network.get("BiddingStrategyType", "UNKNOWN"),
            },
        }
        if is_multi_goal and priority_goals_items is not None:
            strategy_applied["priority_goals"] = [
                {
                    "goal_id": pg["GoalId"],
                    "value_rub": pg["Value"] / self._MICROS_PER_RUBLE,
                }
                for pg in priority_goals_items
            ]
        if payload.bid_ceiling is not None:
            strategy_applied["search"]["bid_ceiling_rub"] = payload.bid_ceiling
            strategy_applied["search"]["bid_ceiling_micros"] = int(
                payload.bid_ceiling * self._MICROS_PER_RUBLE
            )

        # Mock mode
        if not is_live:
            audit = self.append_audit(
                "yandex_strategy_requested",
                campaign_id,
                dry_run=payload.dry_run,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "source": "mock",
                    "mode": mode,
                    "strategy_applied": strategy_applied,
                },
            )
            result = YandexStrategyResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=payload.dry_run,
                applied=False,
                source="mock",
                audit_id=audit.id,
                payload_preview=payload_preview,
                strategy_applied=strategy_applied,
                readback=None,
                provider_warnings=[],
            )
            self._strategy_results_by_key[cache_key] = result
            return result

        # Live modes: dry-run
        if payload.dry_run:
            audit = self.append_audit(
                "yandex_strategy_requested",
                campaign_id,
                dry_run=True,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "source": "yandex",
                    "mode": mode,
                    "payload_redacted": payload_preview,
                },
            )
            result = YandexStrategyResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=True,
                applied=False,
                source="yandex",
                audit_id=audit.id,
                payload_preview=payload_preview,
                strategy_applied=strategy_applied,
                readback=None,
                provider_warnings=[],
            )
            self._strategy_results_by_key[cache_key] = result
            return result

        # Real apply: only live_write
        if not can_write:
            raise YandexDirectError(
                f"Live writes require DIRECTPILOT_MODE=live_write; "
                f"current mode is {mode!r}; refusing to dispatch "
                f"campaigns.update"
            )
        if client is None:
            raise YandexDirectError(
                "YandexDirectClient is required for live strategy writes"
            )

        # Fail closed if budget read was ambiguous.
        if is_live and not daily_budget_read_ok:
            raise YandexDirectError(
                "Could not read current DailyBudget from campaign "
                f"{campaign_id!r}; refusing to send campaigns.update"
            )

        try:
            yandex_result = client.campaigns_update_strategy(
                campaign_id,
                text_campaign_block,
                daily_budget=current_daily_budget,
            )
            if not yandex_result.get("ok"):
                err = yandex_result.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected campaigns.update: "
                    f"error_code={err.get('error_code')!r}",
                    diagnostics={
                        "error_code": err.get("error_code"),
                        "error_detail": err.get("error_detail"),
                        "payload_preview": payload_preview,
                    },
                )

            # Readback: re-read the strategy to verify.
            readback_block: dict[str, Any] | None = None
            provider_warnings: list[Any] = _provider_warnings_from_result(
                yandex_result
            )
            try:
                readback_response = client.campaigns_get_full_strategy(campaign_id)
                if readback_response.get("ok"):
                    rb_result = readback_response.get("result") or {}
                    if isinstance(rb_result, dict):
                        rb_campaigns = rb_result.get("Campaigns") or []
                        if rb_campaigns and isinstance(rb_campaigns[0], dict):
                            tc_rb = rb_campaigns[0].get("TextCampaign")
                            if isinstance(tc_rb, dict):
                                bs = tc_rb.get("BiddingStrategy")
                                if isinstance(bs, dict):
                                    readback_block = {"BiddingStrategy": dict(bs)}
            except Exception:
                pass  # Best-effort readback

            audit = self.append_audit(
                "yandex_strategy_requested",
                campaign_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "source": "yandex",
                    "mode": mode,
                    "strategy_applied": strategy_applied,
                    "yandex_units": yandex_result.get("units"),
                    "readback_present": readback_block is not None,
                },
            )
            result = YandexStrategyResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=False,
                applied=True,
                source="yandex",
                audit_id=audit.id,
                payload_preview=None,
                strategy_applied=strategy_applied,
                readback=readback_block,
                provider_warnings=provider_warnings,
            )
            self._strategy_results_by_key[cache_key] = result
            return result
        except YandexDirectError as exc:
            self.append_audit(
                "yandex_strategy_failed",
                campaign_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "source": "yandex",
                    "mode": mode,
                    "yandex_error": str(exc),
                    "yandex_error_detail": exc.diagnostics.get(
                        "error_detail"
                    ),
                    "payload_preview": exc.diagnostics.get(
                        "payload_preview"
                    ),
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
    def _resolve_utm_href(
        href: str,
        *,
        draft: "CampaignDraft",
        ad_id: str = "",
        utm_config_override: "UtmConfig | None" = None,
    ) -> str:
        """Apply UTM params to *href* using the draft's ``utm_config``
        or an explicit override.

        When ``utm_config_override`` is provided, it takes precedence
        over ``draft.utm_config``.  This allows ``LiveCreateCampaignRequest``
        to supply a different UTM config at creation time without
        mutating the stored draft.

        When the effective UTM config is None or ``enabled=False``,
        returns *href* unchanged.  Otherwise builds the UTM-tagged URL
        using the same builder as the UTM audit/plan/apply endpoints.

        *ad_id* is used as ``utm_content`` (the ad's local draft id).
        For live-create, this is the local ad id — the Yandex ad id is
        not yet known at creation time.  The response and docs surface
        this limitation.
        """
        utm = utm_config_override if utm_config_override is not None else draft.utm_config
        if utm is None or not utm.enabled:
            return href
        slug = utm.campaign_slug or generate_campaign_slug(
            draft.name or draft.business_type or "campaign", draft.id
        )
        return build_utm_url(
            href,
            utm_source="yandex",
            utm_medium="cpc",
            utm_campaign=slug,
            utm_content=ad_id or "{ad_id}",
            utm_term="",  # keyword-level mapping not yet implemented
            overwrite=utm.overwrite,
            custom_params=utm.custom_params,
        )

    @staticmethod
    def _build_v5_chain_payloads(
        draft: "CampaignDraft",
        *,
        campaign_id: int | str | None,
        utm_config_override: "UtmConfig | None" = None,
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
            if phrase and "/" not in phrase
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
            href = MockStore._resolve_utm_href(ad.landing_url, draft=draft, ad_id=ad.id, utm_config_override=utm_config_override)
            text_ad: dict[str, Any] = {
                "Title": ad.title,
                "Text": ad.text,
                "Href": href,
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
            draft, campaign_id=None, utm_config_override=payload.utm_config
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
                draft, campaign_id=new_campaign_id, utm_config_override=payload.utm_config
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

    # ------------------------------------------------------------------
    # Live existing-campaign ads — add ads to an existing ad group
    # ------------------------------------------------------------------

    def yandex_ad_group_ads_add(
        self,
        ad_group_id: str,
        payload: LiveAdCreateRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> LiveAdCreateResult:
        """Add text ads to an existing live ad group via v5 ``ads.add``.

        Gate contract: identical to the rest of the product surface
        (``approved`` + ``idempotency_key`` + ``dry_run``; real apply
        only in ``live_write``).

        Preflight checks (dry_run and apply):
        * Duplicate title/text/href detection among existing ads
          in the same ad group when client is available.
        * Warnings for optional inheritance: BusinessId, SitelinkSetId,
          and note that keywords/negative keywords are managed
          separately at campaign/ad-group level.
        """
        if not payload.approved:
            raise ValueError("Action requires explicit approval")

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"

        # Idempotency cache.
        cache_key = (
            f"adgroup_ads_add:{ad_group_id}:{payload.idempotency_key}:"
            f"{'dry' if payload.dry_run else 'apply'}"
        )
        cached = getattr(self, "adgroup_ads_add_results_by_key", None)
        if cached is None:
            cached = {}
            self.adgroup_ads_add_results_by_key = cached
        if cache_key in cached:
            return cached[cache_key]

        # Pre-flight mode gate for apply.
        if not payload.dry_run and not can_write:
            raise YandexDirectError(
                f"Live writes require DIRECTPILOT_MODE=live_write; "
                f"current mode is {mode!r}; refusing to dispatch ads.add"
            )

        warnings: list[LiveAdCreateWarning] = []

        # Preflight: try to read existing context for the ad group.
        existing_business_ids: set[int] = set()
        existing_sitelink_set_ids: set[int] = set()
        existing_ads_texts: list[dict[str, str]] = []
        campaign_id: str | None = None

        if client is not None and is_live:
            try:
                # We need the campaign id from the ad group id.
                # First try to read ads by ad group ids — unfortunately
                # ads.get doesn't accept AdGroupIds in SelectionCriteria,
                # only CampaignIds or Ids. We'll infer via a broader read
                # if the ad_group_id is numeric and looks like a Direct id.
                _adg_int = _try_int(ad_group_id)
                if _adg_int is not None:
                    # We can't efficiently look up the campaign from just
                    # an ad group id without traversing. Skip the full
                    # preflight read for now — the caller is expected to
                    # have the campaign context. We'll still surface
                    # the warnings/questions fields as prompts.
                    pass
            except Exception:
                # Preflight read failure is non-blocking.
                pass

        # Always emit inheritance warnings as prompts for the marketer.
        warnings.append(
            LiveAdCreateWarning(
                code="inherit_business_id",
                message=(
                    "Existing ads in this campaign may use a BusinessId. "
                    "If you omit business_id, the new ads will have no "
                    "organization attached. Consider reusing the same "
                    "BusinessId as existing ads."
                ),
                severity="info",
            )
        )
        warnings.append(
            LiveAdCreateWarning(
                code="inherit_sitelink_set_id",
                message=(
                    "Existing ads may use a SitelinkSetId for quick links. "
                    "If you omit sitelink_set_id, the new ads will have no "
                    "quick links. Consider reusing the same set if relevant."
                ),
                severity="info",
            )
        )
        warnings.append(
            LiveAdCreateWarning(
                code="keywords_not_per_ad",
                message=(
                    "Keywords and negative keywords are managed at the "
                    "campaign / ad-group level, NOT per ad. If the new ad "
                    "angle needs extra keywords or minuses, propose a "
                    "separate semantic-change task — do not mix with ad "
                    "creation."
                ),
                severity="info",
            )
        )

        # Build the v5 ads.add payload.
        v5_ads: list[dict[str, Any]] = []
        _adg_int = _try_int(ad_group_id)
        for item in payload.ads:
            text_ad: dict[str, Any] = {
                "Title": item.title,
                "Text": item.text,
                "Href": item.href,
            }
            if item.title2:
                text_ad["Title2"] = item.title2
            if item.sitelink_set_id is not None:
                text_ad["SitelinkSetId"] = item.sitelink_set_id
            if item.business_id is not None:
                text_ad["BusinessId"] = item.business_id
            if item.prefer_vcard_over_business is not None:
                text_ad["PreferVCardOverBusiness"] = item.prefer_vcard_over_business
            ad_entry: dict[str, Any] = {"TextAd": text_ad}
            if _adg_int is not None:
                ad_entry["AdGroupId"] = _adg_int
            v5_ads.append(ad_entry)

        payload_preview = {
            "method": "ads.add",
            "params": {"Ads": _redact_ads_payload(v5_ads)},
        }

        # Dry-run: never touch the network.
        if payload.dry_run:
            audit = self.append_audit(
                "yandex_ad_group_ads_add_requested",
                ad_group_id,
                dry_run=True,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex" if is_live else "mock",
                    "reason": payload.reason,
                    "ad_count": len(v5_ads),
                    "warnings": [w.model_dump() for w in warnings],
                    "stage": "dry_run_preview",
                },
            )
            result = LiveAdCreateResult(
                dry_run=True,
                applied=False,
                source="yandex" if is_live else "mock",
                mode=mode,
                audit_id=audit.id,
                ad_group_id=ad_group_id,
                payload_preview=payload_preview,
                warnings=warnings,
            )
            return result

        # Real apply gate.
        if not can_write:
            raise YandexDirectError(
                "Live writes are not allowed in live_readonly mode; "
                "switch DIRECTPILOT_MODE to live_write to add ads"
            )
        if client is None:
            raise YandexDirectError(
                "YandexDirectClient is required for live ad-group/ads writes"
            )

        try:
            yandex_result = client.ads_add(v5_ads)
            if not yandex_result.get("ok"):
                err = yandex_result.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected ads.add: "
                    f"error_code={err.get('error_code')!r}"
                )
            sent_units = _safe_units(yandex_result.get("units"))
            provider_warnings = _provider_warnings_from_result(yandex_result)

            # Extract ad ids from AddResults.
            add_results = (
                (yandex_result.get("result") or {}).get("AddResults") or []
            )
            ad_ids: list[int] = []
            for ar in add_results:
                if isinstance(ar, dict) and "Id" in ar:
                    ad_ids.append(ar["Id"])

            # Readback if feasible.
            readback = None
            if ad_ids:
                try:
                    rb_result = client.ads_get_by_ids(ad_ids)
                    if rb_result.get("ok"):
                        readback = (rb_result.get("result") or {}).get("Ads") or []
                except Exception:
                    pass

            audit = self.append_audit(
                "yandex_ad_group_ads_add_applied",
                ad_group_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex",
                    "reason": payload.reason,
                    "ad_count": len(v5_ads),
                    "ad_ids": ad_ids,
                    "yandex_units": sent_units,
                    "warnings": [w.model_dump() for w in warnings],
                    "provider_warnings": [pw.model_dump() for pw in provider_warnings],
                    "stage": "ads.add",
                    "applied": True,
                },
            )
            result = LiveAdCreateResult(
                dry_run=False,
                applied=True,
                source="yandex",
                mode=mode,
                audit_id=audit.id,
                ad_group_id=ad_group_id,
                ad_ids=ad_ids,
                add_results=add_results,
                readback=readback,
                payload_preview=payload_preview,
                warnings=warnings,
                provider_warnings=provider_warnings,
                yandex_units=sent_units,
            )
            cached[cache_key] = result
            return result
        except YandexDirectError as exc:
            self.append_audit(
                "yandex_ad_group_ads_add_failed",
                ad_group_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "ad_count": len(v5_ads),
                    "yandex_error": str(exc),
                },
            )
            raise
        except Exception as exc:  # noqa: BLE001
            safe = YandexDirectError(
                f"unexpected error during ad-group/ads add: "
                f"{type(exc).__name__}: {exc}"
            )
            self.append_audit(
                "yandex_ad_group_ads_add_failed",
                ad_group_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "ad_count": len(v5_ads),
                    "yandex_error": str(safe),
                    "exception_type": type(exc).__name__,
                },
            )
            raise safe from exc

    # ------------------------------------------------------------------
    # ads.moderate — send ads to moderation
    # ------------------------------------------------------------------

    def yandex_ads_moderate(
        self,
        payload: AdsModerateRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> AdsModerateResult:
        """Send ads to moderation via v5 ``ads.moderate``.

        Gate contract identical to other write endpoints:
        ``dry_run=True`` is preview-only; ``dry_run=False`` requires
        ``approved=True``, ``idempotency_key``, ``DIRECTPILOT_MODE=live_write``.
        """
        if not payload.approved:
            raise ValueError("Action requires explicit approval")

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"

        # Idempotency cache.
        ad_ids_key = tuple(sorted(payload.ad_ids))
        cache_key = (
            f"ads_moderate:{ad_ids_key}:{payload.idempotency_key}:"
            f"{'dry' if payload.dry_run else 'apply'}"
        )
        cached = getattr(self, "ads_moderate_results_by_key", None)
        if cached is None:
            cached = {}
            self.ads_moderate_results_by_key = cached
        if cache_key in cached:
            return cached[cache_key]

        # Pre-flight mode gate for apply.
        if not payload.dry_run and not can_write:
            raise YandexDirectError(
                f"Live writes require DIRECTPILOT_MODE=live_write; "
                f"current mode is {mode!r}; refusing to dispatch ads.moderate"
            )

        payload_preview = {
            "method": "ads.moderate",
            "params": {
                "SelectionCriteria": {"Ids": payload.ad_ids},
            },
        }

        # Dry-run path.
        if payload.dry_run:
            audit = self.append_audit(
                "yandex_ads_moderate_requested",
                "ads",
                dry_run=True,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex" if is_live else "mock",
                    "reason": payload.reason,
                    "ad_ids": payload.ad_ids,
                    "ad_count": len(payload.ad_ids),
                    "stage": "dry_run_preview",
                },
            )
            result = AdsModerateResult(
                dry_run=True,
                applied=False,
                source="yandex" if is_live else "mock",
                mode=mode,
                audit_id=audit.id,
                ad_ids=list(payload.ad_ids),
                payload_preview=payload_preview,
            )
            return result

        # Real apply gate.
        if not can_write:
            raise YandexDirectError(
                "Live writes are not allowed in live_readonly mode; "
                "switch DIRECTPILOT_MODE to live_write to moderate ads"
            )
        if client is None:
            raise YandexDirectError(
                "YandexDirectClient is required for live ads.moderate writes"
            )

        try:
            yandex_result = client.ads_moderate(payload.ad_ids)
            if not yandex_result.get("ok"):
                err = yandex_result.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected ads.moderate: "
                    f"error_code={err.get('error_code')!r}"
                )
            sent_units = _safe_units(yandex_result.get("units"))
            provider_warnings = _provider_warnings_from_result(yandex_result)

            moderate_results = (
                (yandex_result.get("result") or {}).get("ModerateResults") or []
            )

            # Readback if feasible.
            readback = None
            try:
                rb_result = client.ads_get_by_ids(payload.ad_ids)
                if rb_result.get("ok"):
                    readback = (rb_result.get("result") or {}).get("Ads") or []
            except Exception:
                pass

            audit = self.append_audit(
                "yandex_ads_moderate_applied",
                "ads",
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex",
                    "reason": payload.reason,
                    "ad_ids": payload.ad_ids,
                    "ad_count": len(payload.ad_ids),
                    "yandex_units": sent_units,
                    "provider_warnings": [pw.model_dump() for pw in provider_warnings],
                    "stage": "ads.moderate",
                    "applied": True,
                },
            )
            result = AdsModerateResult(
                dry_run=False,
                applied=True,
                source="yandex",
                mode=mode,
                audit_id=audit.id,
                ad_ids=list(payload.ad_ids),
                moderate_results=moderate_results,
                readback=readback,
                payload_preview=payload_preview,
                provider_warnings=provider_warnings,
                yandex_units=sent_units,
            )
            cached[cache_key] = result
            return result
        except YandexDirectError as exc:
            self.append_audit(
                "yandex_ads_moderate_failed",
                "ads",
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "ad_ids": payload.ad_ids,
                    "yandex_error": str(exc),
                },
            )
            raise
        except Exception as exc:  # noqa: BLE001
            safe = YandexDirectError(
                f"unexpected error during ads.moderate: "
                f"{type(exc).__name__}: {exc}"
            )
            self.append_audit(
                "yandex_ads_moderate_failed",
                "ads",
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "ad_ids": payload.ad_ids,
                    "yandex_error": str(safe),
                    "exception_type": type(exc).__name__,
                },
            )
            raise safe from exc

    # ------------------------------------------------------------------
    # Autotargeting settings read / update
    # ------------------------------------------------------------------

    def yandex_autotargeting_read(
        self,
        campaign_id: str,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> "YandexAutotargetingReadResult":
        """Read autotargeting settings for all ad groups in a campaign.

        Uses ``keywords.get`` with ``AutotargetingSettingsCategoriesFieldNames``
        and ``AutotargetingSettingsBrandOptionsFieldNames`` to extract the
        current autotargeting configuration from each ``---autotargeting`` row.
        """
        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")

        if is_live and client is not None:
            try:
                kw_response = client.keywords_get_autotargeting(campaign_id)
            except YandexDirectError as exc:
                raise exc
            if not kw_response.get("ok"):
                err = kw_response.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected keywords.get (autotargeting): "
                    f"error_code={err.get('error_code')!r}"
                )

            kw_result = kw_response.get("result") or {}
            raw_keywords = kw_result.get("Keywords") or []

            # Also read ad groups for names
            try:
                ag_response = client.adgroups_get(campaign_id)
            except YandexDirectError:
                ag_response = None

            raw_adgroups = (
                (ag_response.get("result") or {}).get("AdGroups") or []
                if ag_response and ag_response.get("ok")
                else []
            )

            ag_name_by_id: dict[str, str] = {}
            for ag in raw_adgroups:
                if isinstance(ag, dict):
                    agid = str(ag.get("Id") or ag.get("id") or "")
                    ag_name_by_id[agid] = str(
                        ag.get("Name") or ag.get("name") or ""
                    )

            read_items: list[YandexAutotargetingReadItem] = []
            for kw in raw_keywords:
                if not isinstance(kw, dict):
                    continue
                keyword_text = str(kw.get("Keyword") or "")
                if keyword_text != "---autotargeting":
                    continue

                kw_id = str(kw.get("Id") or kw.get("id") or "")
                ag_id = str(kw.get("AdGroupId") or kw.get("adGroupId") or "")

                # Extract categories from the keyword row
                raw_cats = kw.get("AutotargetingSettingsCategories")
                categories: dict[str, str] | None = None
                if isinstance(raw_cats, dict):
                    categories = {
                        k: str(raw_cats[k]) if k in raw_cats else "NO"
                        for k in AUTOTARGETING_CATEGORIES
                    }

                raw_brands = kw.get("AutotargetingSettingsBrandOptions")
                brand_options: dict[str, str] | None = None
                if isinstance(raw_brands, dict):
                    brand_options = {
                        k: str(raw_brands[k]) if k in raw_brands else "NO"
                        for k in AUTOTARGETING_BRAND_OPTIONS
                    }

                read_items.append(
                    YandexAutotargetingReadItem(
                        ad_group_id=ag_id,
                        ad_group_name=ag_name_by_id.get(ag_id, ""),
                        autotargeting_keyword_id=kw_id,
                        status=str(kw.get("Status") or kw.get("status") or "UNKNOWN"),
                        state=str(kw.get("State") or kw.get("state") or None) if kw.get("State") is not None else None,
                        serving_status=str(kw.get("ServingStatus") or kw.get("servingStatus") or None) if kw.get("ServingStatus") is not None else None,
                        categories=categories,
                        brand_options=brand_options,
                        raw_provider=dict(kw),
                    )
                )

            return YandexAutotargetingReadResult(
                campaign_id=campaign_id,
                source="yandex",
                read_only=True,
                ad_groups=read_items,
                default_preset="exact_narrow",
            )

        # Mock fallback
        from app.yandex_facade import mock_yandex
        mock_data = mock_yandex.list_autotargeting(campaign_id)
        return YandexAutotargetingReadResult(
            campaign_id=campaign_id,
            source="mock",
            read_only=True,
            ad_groups=[
                YandexAutotargetingReadItem(**item)
                for item in mock_data
            ],
            default_preset="exact_narrow",
        )

    def yandex_autotargeting_update(
        self,
        campaign_id: str,
        payload: "YandexAutotargetingRequest",
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> "YandexAutotargetingResult":
        """Apply autotargeting settings to an existing campaign via v5 ``keywords.update``.

        Dry-run returns the exact ``keywords.update`` payload preview without
        touching the network. Live apply reads current autotargeting rows
        via ``keywords.get``, builds the update payload with all five
        category booleans + all three brand booleans explicitly, and
        dispatches ``keywords.update``.
        """
        if not payload.approved:
            raise ValueError("Action requires explicit approval")

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"

        # Resolve effective settings
        categories = payload.resolve_categories()
        brand_options = payload.resolve_brand_options()

        # Idempotency cache check (mirrors strategy/time-targeting).
        cache_key = f"autotargeting:{campaign_id}:{payload.idempotency_key}"
        if cache_key in self._autotargeting_results_by_key:
            cached = self._autotargeting_results_by_key[cache_key]
            if cached.dry_run != payload.dry_run:
                raise YandexDirectError(
                    f"Idempotency key {payload.idempotency_key!r} was "
                    f"previously used with dry_run={cached.dry_run}; "
                    f"replay with dry_run={payload.dry_run} is not allowed"
                )
            return cached

        # Read current autotargeting rows
        if is_live and client is not None:
            try:
                kw_response = client.keywords_get_autotargeting(campaign_id)
            except YandexDirectError as exc:
                raise exc
            if not kw_response.get("ok"):
                err = kw_response.get("error") or {}
                raise YandexDirectError(
                    f"Yandex Direct rejected keywords.get (autotargeting): "
                    f"error_code={err.get('error_code')!r}"
                )
            kw_result = kw_response.get("result") or {}
            raw_keywords = kw_result.get("Keywords") or []
        elif not is_live:
            # Mock fallback for dry-run preview in mock mode
            from app.yandex_facade import mock_yandex
            mock_data = mock_yandex.list_autotargeting(campaign_id)
            raw_keywords = [
                {"Id": item["autotargeting_keyword_id"],
                 "AdGroupId": item["ad_group_id"],
                 "Keyword": "---autotargeting",
                 "State": item.get("state", "ON"),
                 "Status": item.get("status", "ACCEPTED"),
                 "ServingStatus": item.get("serving_status", "ELIGIBLE")}
                for item in mock_data
            ]
        else:
            raw_keywords = []

        # Filter to autotargeting rows
        autotargeting_rows: list[dict[str, Any]] = []
        for kw in raw_keywords:
            if not isinstance(kw, dict):
                continue
            if str(kw.get("Keyword") or "") == "---autotargeting":
                autotargeting_rows.append(kw)

        # Build a map of ad_group_id -> autotargeting row
        autotargeting_by_ag: dict[str, dict] = {}
        for row in autotargeting_rows:
            ag_id = str(row.get("AdGroupId") or row.get("adGroupId") or "")
            if ag_id:
                autotargeting_by_ag[ag_id] = row

        # Determine which ad groups to target
        if payload.ad_group_ids is not None:
            target_ag_ids = list(payload.ad_group_ids)
        else:
            target_ag_ids = list(autotargeting_by_ag.keys())

        # Build keywords.update payload for existing rows AND (optionally)
        # keywords.add payload for ad groups that lack autotargeting rows.
        update_items: list[dict[str, Any]] = []
        add_items: list[dict[str, Any]] = []
        targeted_ids: list[str] = []
        skipped_ad_group_ids: list[str] = []
        add_targeted_ids: list[str] = []

        for ag_id in target_ag_ids:
            row = autotargeting_by_ag.get(ag_id)
            if row is not None:
                kw_id = row.get("Id") or row.get("id")
                if kw_id is not None:
                    update_items.append({
                        "Id": int(kw_id) if isinstance(kw_id, (int, str)) and str(kw_id).isdigit() else kw_id,
                        "AutotargetingSettings": {
                            "Categories": dict(categories),
                            "BrandOptions": dict(brand_options),
                        },
                    })
                    targeted_ids.append(ag_id)
                else:
                    skipped_ad_group_ids.append(ag_id)
            elif payload.create_missing:
                # Build a keywords.add item with explicit autotargeting settings
                add_items.append({
                    "Keyword": "---autotargeting",
                    "AdGroupId": int(ag_id) if ag_id.isdigit() else ag_id,
                    "AutotargetingSettings": {
                        "Categories": dict(categories),
                        "BrandOptions": dict(brand_options),
                    },
                })
                add_targeted_ids.append(ag_id)
            else:
                skipped_ad_group_ids.append(ag_id)

        # Fail closed if nothing to do
        if not update_items and not add_items:
            raise YandexDirectError(
                "No autotargeting rows found for the specified campaign/ad groups. "
                "Set create_missing=true to create new ---autotargeting rows via "
                "keywords.add, or provide ad_group_ids with existing autotargeting rows."
            )

        payload_preview: dict[str, Any] = {
            "update": {
                "method": "update",
                "params": {"Keywords": update_items},
            },
        }
        if add_items:
            payload_preview["add"] = {
                "method": "add",
                "params": {"Keywords": add_items},
            }

        # Dry-run preview
        if payload.dry_run:
            audit = self.append_audit(
                "yandex_autotargeting_requested",
                campaign_id,
                dry_run=True,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex" if is_live else "mock",
                    "reason": payload.reason,
                    "preset": payload.preset,
                    "categories_sent": categories,
                    "brand_options_sent": brand_options,
                    "targeted_ad_group_ids": targeted_ids,
                    "add_targeted_ad_group_ids": add_targeted_ids,
                    "skipped_ad_group_ids": skipped_ad_group_ids,
                    "updated_keyword_ids": [
                        str(item["Id"]) for item in update_items
                    ],
                    "add_items_count": len(add_items),
                    "create_missing": payload.create_missing,
                    "stage": "dry_run_preview",
                },
            )
            result = YandexAutotargetingResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=True,
                applied=False,
                source="yandex" if is_live else "mock",
                audit_id=audit.id,
                preset=payload.preset,
                categories_applied=categories,
                brand_options_applied=brand_options,
                targeted_ad_group_ids=targeted_ids + add_targeted_ids,
                skipped_ad_group_ids=skipped_ad_group_ids,
                updated_keyword_ids=[str(item["Id"]) for item in update_items],
                created_keyword_ids=[],
                payload_preview=payload_preview,
            )
            self._autotargeting_results_by_key[cache_key] = result
            return result

        # Apply gate
        if not can_write:
            raise YandexDirectError(
                f"Live writes require DIRECTPILOT_MODE=live_write; "
                f"current mode is {mode!r}; refusing to dispatch autotargeting mutations"
            )
        if client is None:
            raise YandexDirectError(
                "YandexDirectClient is required for live autotargeting writes"
            )

        try:
            total_units = 0
            provider_warnings: list[Any] = []
            created_kw_ids: list[str] = []

            # Create new autotargeting rows via keywords.add (if create_missing=true)
            if add_items:
                yandex_result = client.keywords_add(add_items)
                if not yandex_result.get("ok"):
                    err = yandex_result.get("error") or {}
                    raise YandexDirectError(
                        f"Yandex Direct rejected keywords.add (autotargeting): "
                        f"error_code={err.get('error_code')!r}"
                    )
                total_units += _safe_units(yandex_result.get("units"))
                provider_warnings.extend(_provider_warnings_from_result(yandex_result))
                # Extract ids from AddResults
                add_result = yandex_result.get("result") or {}
                add_results_list = add_result.get("AddResults") or []
                for ar in add_results_list:
                    if isinstance(ar, dict) and ar.get("Id") is not None:
                        created_kw_ids.append(str(ar["Id"]))

            # Update existing autotargeting rows
            if update_items:
                yandex_result = client.keywords_update(update_items)
                if not yandex_result.get("ok"):
                    err = yandex_result.get("error") or {}
                    raise YandexDirectError(
                        f"Yandex Direct rejected keywords.update: "
                        f"error_code={err.get('error_code')!r}"
                    )
                total_units += _safe_units(yandex_result.get("units"))
                provider_warnings.extend(_provider_warnings_from_result(yandex_result))

            sent_units = total_units

            stages = []
            if add_items:
                stages.append("keywords.add")
            if update_items:
                stages.append("keywords.update")

            audit = self.append_audit(
                "yandex_autotargeting_applied",
                campaign_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "source": "yandex",
                    "reason": payload.reason,
                    "preset": payload.preset,
                    "categories_sent": categories,
                    "brand_options_sent": brand_options,
                    "targeted_ad_group_ids": targeted_ids,
                    "add_targeted_ad_group_ids": add_targeted_ids,
                    "skipped_ad_group_ids": skipped_ad_group_ids,
                    "updated_keyword_ids": [
                        str(item["Id"]) for item in update_items
                    ],
                    "created_keyword_ids": created_kw_ids,
                    "create_missing": payload.create_missing,
                    "add_items_count": len(add_items),
                    "yandex_units": sent_units,
                    "provider_warnings": [pw.model_dump() for pw in provider_warnings],
                    "stage": "+".join(stages) if stages else "none",
                    "applied": True,
                },
            )
            result = YandexAutotargetingResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=False,
                applied=True,
                source="yandex",
                audit_id=audit.id,
                preset=payload.preset,
                categories_applied=categories,
                brand_options_applied=brand_options,
                targeted_ad_group_ids=targeted_ids + add_targeted_ids,
                skipped_ad_group_ids=skipped_ad_group_ids,
                updated_keyword_ids=[str(item["Id"]) for item in update_items],
                created_keyword_ids=created_kw_ids,
                provider_warnings=provider_warnings,
                yandex_units=sent_units,
            )
            self._autotargeting_results_by_key[cache_key] = result
            return result
        except YandexDirectError as exc:
            self.append_audit(
                "yandex_autotargeting_failed",
                campaign_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "yandex_error": str(exc),
                },
            )
            raise
        except Exception as exc:
            safe = YandexDirectError(
                f"unexpected error during autotargeting update: "
                f"{type(exc).__name__}: {exc}"
            )
            self.append_audit(
                "yandex_autotargeting_failed",
                campaign_id,
                dry_run=False,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "yandex_error": str(safe),
                    "exception_type": type(exc).__name__,
                },
            )
            raise safe from exc


    @staticmethod
    def _auction_forecast_strict_int(value: Any) -> int | None:
        """Accept only JSON integer values, never bools, floats, or strings."""

        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _auction_forecast_unavailable_reason(item: AuctionForecastItem) -> str:
        if item.serving_status == "RARELY_SERVED":
            return "RARELY_SERVED"
        if item.state != "ON" or item.status != "ACCEPTED":
            return "KEYWORD_NOT_SERVING"
        return "NO_AUCTION_DATA"

    @staticmethod
    def _set_auction_forecast_status(
        items_by_keyword_id: dict[int, list[AuctionForecastItem]],
        keyword_id: int,
        *,
        status: Literal["AVAILABLE", "NOT_APPLICABLE", "UNAVAILABLE", "ERROR"],
        reason: str | None,
        auction_bids: list[AuctionForecastAuctionBid] | None = None,
    ) -> None:
        for item in items_by_keyword_id.get(keyword_id, []):
            item.forecast_status = status
            item.forecast_reason = reason
            item.auction_bids = list(auction_bids or [])

    def yandex_auction_forecast(
        self,
        campaign_id: str,
        *,
        client: YandexDirectClient,
        keyword_ids: list[int] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> AuctionForecastResult:
        """Build a read-only, page-scoped forecast from Direct v5 data."""

        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("page_token must be a non-negative decimal offset")
        if not campaign_id.isascii() or not campaign_id.isdecimal() or int(campaign_id) <= 0:
            raise ValueError("campaign_id must be a positive integer")
        expected_campaign_id = int(campaign_id)

        requested_ids: list[int] = []
        seen_requested_ids: set[int] = set()
        for keyword_id in keyword_ids or []:
            if self._auction_forecast_strict_int(keyword_id) is None or keyword_id <= 0:
                raise ValueError("keyword_ids must contain positive integers")
            if keyword_id not in seen_requested_ids:
                requested_ids.append(keyword_id)
                seen_requested_ids.add(keyword_id)
        if len(requested_ids) > 1000:
            raise ValueError("keyword_ids must contain at most 1000 ids")

        keywords_response = client.keywords_get(
            campaign_id,
            keyword_ids=requested_ids or None,
            limit=limit,
            offset=offset,
        )
        if not keywords_response.get("ok"):
            raise YandexDirectError("keywords.get failed before auction forecast")
        keyword_result = keywords_response.get("result")
        if not isinstance(keyword_result, dict):
            raise YandexDirectError("keywords.get returned an invalid result envelope")
        raw_keywords = keyword_result.get("Keywords")
        if not isinstance(raw_keywords, list):
            raise YandexDirectError("keywords.get did not enumerate a keyword page")

        items: list[AuctionForecastItem] = []
        items_by_keyword_id: dict[int, list[AuctionForecastItem]] = {}
        manual_ids: list[int] = []
        seen_manual_ids: set[int] = set()
        for raw_keyword in raw_keywords:
            if not isinstance(raw_keyword, dict):
                raise YandexDirectError("keywords.get returned an invalid keyword row")
            keyword_id = self._auction_forecast_strict_int(raw_keyword.get("Id"))
            if keyword_id is None:
                raise YandexDirectError("keywords.get returned a keyword without an integer id")
            current_bid = self._auction_forecast_strict_int(raw_keyword.get("Bid"))
            phrase = raw_keyword.get("Keyword") if isinstance(raw_keyword.get("Keyword"), str) else None
            is_autotargeting = phrase == "---autotargeting"
            item = AuctionForecastItem(
                keyword_id=keyword_id,
                ad_group_id=self._auction_forecast_strict_int(raw_keyword.get("AdGroupId")),
                phrase=None if is_autotargeting else phrase,
                state=raw_keyword.get("State") if isinstance(raw_keyword.get("State"), str) else None,
                status=raw_keyword.get("Status") if isinstance(raw_keyword.get("Status"), str) else None,
                serving_status=(
                    raw_keyword.get("ServingStatus")
                    if isinstance(raw_keyword.get("ServingStatus"), str)
                    else None
                ),
                current_search_bid_micros=current_bid,
                current_search_bid_rub=(current_bid / 1_000_000 if current_bid is not None else None),
                forecast_status="ERROR",
                forecast_reason="INVALID_AUCTION_DATA",
            )
            items.append(item)
            items_by_keyword_id.setdefault(keyword_id, []).append(item)

            source_campaign_id = self._auction_forecast_strict_int(raw_keyword.get("CampaignId"))
            if source_campaign_id != expected_campaign_id:
                continue
            if is_autotargeting:
                self._set_auction_forecast_status(
                    items_by_keyword_id,
                    keyword_id,
                    status="NOT_APPLICABLE",
                    reason="AUTOTARGETING",
                )
                continue
            if keyword_id not in seen_manual_ids:
                manual_ids.append(keyword_id)
                seen_manual_ids.add(keyword_id)

        limited_by = self._auction_forecast_strict_int(keyword_result.get("LimitedBy"))
        next_page_token = str(limited_by) if limited_by is not None and limited_by > offset else None
        result = AuctionForecastResult(
            campaign_id=campaign_id,
            items=items,
            next_page_token=next_page_token,
        )
        if not manual_ids:
            return result

        strategy_response = client.campaigns_get_strategy(campaign_id)
        if not strategy_response.get("ok"):
            raise YandexDirectError("campaign strategy read failed before auction forecast")
        strategy_result = strategy_response.get("result")
        campaigns = strategy_result.get("Campaigns") if isinstance(strategy_result, dict) else None
        campaign = campaigns[0] if isinstance(campaigns, list) and campaigns else None
        if not isinstance(campaign, dict):
            raise YandexDirectError("campaign strategy result is unavailable before auction forecast")
        strategy = campaign.get("TextCampaign", {}).get("BiddingStrategy", {})
        search = strategy.get("Search") if isinstance(strategy, dict) else None
        search_type = (
            search.get("BiddingStrategyType") or search.get("Type")
            if isinstance(search, dict)
            else None
        )
        if search_type == "SERVING_OFF":
            for keyword_id in manual_ids:
                self._set_auction_forecast_status(
                    items_by_keyword_id,
                    keyword_id,
                    status="UNAVAILABLE",
                    reason="SEARCH_SERVING_OFF",
                )
            return result

        for start in range(0, len(manual_ids), _AUCTION_FORECAST_BATCH_SIZE):
            batch = manual_ids[start : start + _AUCTION_FORECAST_BATCH_SIZE]
            batch_ids = set(batch)
            try:
                batch_response = client.keywordbids_get(
                    campaign_id,
                    keyword_ids=batch,
                    limit=_AUCTION_FORECAST_BATCH_SIZE,
                    offset=0,
                    include_auction_bids=True,
                    include_coverage=False,
                )
            except YandexDirectError:
                for keyword_id in batch:
                    self._set_auction_forecast_status(
                        items_by_keyword_id,
                        keyword_id,
                        status="ERROR",
                        reason="UPSTREAM_BATCH_ERROR",
                    )
                continue
            if not batch_response.get("ok"):
                for keyword_id in batch:
                    self._set_auction_forecast_status(
                        items_by_keyword_id,
                        keyword_id,
                        status="ERROR",
                        reason="UPSTREAM_BATCH_ERROR",
                    )
                continue
            batch_result = batch_response.get("result")
            raw_rows = batch_result.get("KeywordBids") if isinstance(batch_result, dict) else None
            if not isinstance(raw_rows, list):
                for keyword_id in batch:
                    self._set_auction_forecast_status(
                        items_by_keyword_id,
                        keyword_id,
                        status="ERROR",
                        reason="INVALID_AUCTION_DATA",
                    )
                continue

            rows_by_keyword_id: dict[int, dict[str, Any]] = {}
            duplicate_ids: set[int] = set()
            for raw_row in raw_rows:
                if not isinstance(raw_row, dict):
                    continue
                keyword_id = self._auction_forecast_strict_int(raw_row.get("KeywordId"))
                if keyword_id not in batch_ids:
                    continue
                if keyword_id in rows_by_keyword_id:
                    duplicate_ids.add(keyword_id)
                    continue
                rows_by_keyword_id[keyword_id] = raw_row

            for keyword_id in duplicate_ids:
                self._set_auction_forecast_status(
                    items_by_keyword_id,
                    keyword_id,
                    status="ERROR",
                    reason="INVALID_AUCTION_DATA",
                )
            for keyword_id in batch_ids - set(rows_by_keyword_id):
                self._set_auction_forecast_status(
                    items_by_keyword_id,
                    keyword_id,
                    status="ERROR",
                    reason="AUCTION_ROW_MISSING",
                )

            for keyword_id, raw_row in rows_by_keyword_id.items():
                if keyword_id in duplicate_ids:
                    continue
                if self._auction_forecast_strict_int(raw_row.get("CampaignId")) != expected_campaign_id:
                    self._set_auction_forecast_status(
                        items_by_keyword_id,
                        keyword_id,
                        status="ERROR",
                        reason="INVALID_AUCTION_DATA",
                    )
                    continue
                raw_search = raw_row.get("Search")
                if not isinstance(raw_search, dict):
                    self._set_auction_forecast_status(
                        items_by_keyword_id,
                        keyword_id,
                        status="ERROR",
                        reason="INVALID_AUCTION_DATA",
                    )
                    continue
                raw_auction = raw_search.get("AuctionBids")
                if raw_auction is None:
                    for item in items_by_keyword_id.get(keyword_id, []):
                        self._set_auction_forecast_status(
                            items_by_keyword_id,
                            keyword_id,
                            status="UNAVAILABLE",
                            reason=self._auction_forecast_unavailable_reason(item),
                        )
                    continue
                if not isinstance(raw_auction, dict):
                    self._set_auction_forecast_status(
                        items_by_keyword_id,
                        keyword_id,
                        status="ERROR",
                        reason="INVALID_AUCTION_DATA",
                    )
                    continue
                raw_auction_items = raw_auction.get("AuctionBidItems")
                if not isinstance(raw_auction_items, list):
                    self._set_auction_forecast_status(
                        items_by_keyword_id,
                        keyword_id,
                        status="ERROR",
                        reason="INVALID_AUCTION_DATA",
                    )
                    continue
                auction_bids: list[AuctionForecastAuctionBid] = []
                malformed = False
                for raw_auction_item in raw_auction_items:
                    if not isinstance(raw_auction_item, dict):
                        malformed = True
                        break
                    traffic_volume = self._auction_forecast_strict_int(raw_auction_item.get("TrafficVolume"))
                    bid_micros = self._auction_forecast_strict_int(raw_auction_item.get("Bid"))
                    price_micros = self._auction_forecast_strict_int(raw_auction_item.get("Price"))
                    if (
                        traffic_volume is None
                        or bid_micros is None
                        or price_micros is None
                        or traffic_volume < 0
                        or bid_micros < 0
                        or price_micros < 0
                    ):
                        malformed = True
                        break
                    auction_bids.append(
                        AuctionForecastAuctionBid(
                            traffic_volume=traffic_volume,
                            bid_micros=bid_micros,
                            bid_rub=bid_micros / 1_000_000,
                            price_micros=price_micros,
                            price_rub=price_micros / 1_000_000,
                        )
                    )
                if malformed:
                    self._set_auction_forecast_status(
                        items_by_keyword_id,
                        keyword_id,
                        status="ERROR",
                        reason="INVALID_AUCTION_DATA",
                    )
                elif auction_bids:
                    self._set_auction_forecast_status(
                        items_by_keyword_id,
                        keyword_id,
                        status="AVAILABLE",
                        reason=None,
                        auction_bids=sorted(auction_bids, key=lambda item: item.traffic_volume),
                    )
                else:
                    self._set_auction_forecast_status(
                        items_by_keyword_id,
                        keyword_id,
                        status="UNAVAILABLE",
                        reason="NO_AUCTION_DATA",
                    )
        return result

    # ---------------------------------- exact auction traffic-level keyword bids
    #
    # This flow deliberately selects only a documented discrete
    # ``AuctionBidItems`` entry with ``TrafficVolume == target``.  It never
    # estimates a UI traffic forecast, interpolates a bid, or falls back to a
    # lower auction level.

    def yandex_keyword_bids_by_traffic_level(
        self,
        campaign_id: str,
        payload: "KeywordBidTrafficLevelRequest",
        *,
        settings: Settings,
        client: YandexDirectClient | None,
    ) -> "KeywordBidTrafficLevelResult":
        """Preview or apply exact discrete Search bids from ``AuctionBids``."""

        if not campaign_id.isascii() or not campaign_id.isdecimal() or int(campaign_id) <= 0:
            raise ValueError("campaign_id must be a positive integer")
        if not payload.approved:
            raise ValueError("Action requires explicit approval")
        if client is None:
            raise YandexDirectError(
                "Yandex Direct client is required to read discrete auction bid levels"
            )
        if not payload.dry_run and settings.directpilot_mode != "live_write":
            raise ValueError("Live writes require DIRECTPILOT_MODE=live_write")

        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "target_traffic_volume": payload.target_traffic_volume,
                    "keyword_ids": sorted(payload.keyword_ids),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cache_key = f"keyword_bid_traffic_level:{campaign_id}:{payload.idempotency_key}"
        cached_record = self._keyword_bid_traffic_level_results_by_key.get(cache_key)
        if cached_record is not None:
            cached = cached_record["result"]
            if cached.dry_run != payload.dry_run:
                raise ValueError(
                    f"Idempotency key {payload.idempotency_key!r} was previously used "
                    f"with dry_run={cached.dry_run}; replay is not allowed"
                )
            if cached_record.get("request_fingerprint") != fingerprint:
                raise ValueError(
                    f"Idempotency key {payload.idempotency_key!r} was previously used "
                    "with a different traffic-level keyword selection"
                )
            return cached

        expected_campaign_id = int(campaign_id)
        strategy_response = client.campaigns_get_strategy(campaign_id)
        if not strategy_response.get("ok"):
            raise YandexDirectError("campaign strategy read failed before traffic-level bid selection")
        strategy_result = strategy_response.get("result")
        campaigns = strategy_result.get("Campaigns") if isinstance(strategy_result, dict) else None
        campaign = campaigns[0] if isinstance(campaigns, list) and campaigns else None
        if not isinstance(campaign, dict):
            raise YandexDirectError("campaign strategy result is unavailable before traffic-level bid selection")
        strategy = campaign.get("TextCampaign", {}).get("BiddingStrategy", {})
        search = strategy.get("Search") if isinstance(strategy, dict) else None
        search_strategy_type = (
            search.get("BiddingStrategyType") or search.get("Type")
            if isinstance(search, dict)
            else None
        )
        # ``HIGHEST_POSITION`` is the only locally documented manual search
        # strategy.  Unknown or automatic strategy types are fail-closed.
        manual_strategy_supported = search_strategy_type == "HIGHEST_POSITION"

        keywords_response = client.keywords_get(campaign_id, keyword_ids=payload.keyword_ids)
        if not keywords_response.get("ok"):
            raise YandexDirectError("keywords.get failed before traffic-level bid selection")
        keyword_result = keywords_response.get("result")
        raw_keywords = keyword_result.get("Keywords") if isinstance(keyword_result, dict) else None
        if not isinstance(raw_keywords, list):
            raise YandexDirectError("keywords.get did not enumerate selected keywords")

        items_by_keyword_id: dict[int, KeywordBidTrafficLevelItem] = {
            keyword_id: KeywordBidTrafficLevelItem(
                keyword_id=keyword_id,
                target_traffic_volume=payload.target_traffic_volume,
                status="UNAVAILABLE",
                reason="KEYWORD_NOT_FOUND",
            )
            for keyword_id in payload.keyword_ids
        }
        raw_keyword_by_id: dict[int, dict[str, Any]] = {}
        duplicate_keyword_ids: set[int] = set()
        selected_keyword_ids = set(payload.keyword_ids)
        for raw_keyword in raw_keywords:
            if not isinstance(raw_keyword, dict):
                continue
            keyword_id = self._auction_forecast_strict_int(raw_keyword.get("Id"))
            if keyword_id not in selected_keyword_ids:
                continue
            if keyword_id in raw_keyword_by_id:
                duplicate_keyword_ids.add(keyword_id)
                continue
            raw_keyword_by_id[keyword_id] = raw_keyword

        eligible_keyword_ids: list[int] = []
        for keyword_id in payload.keyword_ids:
            item = items_by_keyword_id[keyword_id]
            if keyword_id in duplicate_keyword_ids:
                item.status = "FAILED"
                item.reason = "MALFORMED_KEYWORD_RESULT"
                continue
            raw_keyword = raw_keyword_by_id.get(keyword_id)
            if raw_keyword is None:
                continue
            source_campaign_id = self._auction_forecast_strict_int(raw_keyword.get("CampaignId"))
            item.ad_group_id = self._auction_forecast_strict_int(raw_keyword.get("AdGroupId"))
            phrase = raw_keyword.get("Keyword")
            item.phrase = phrase if isinstance(phrase, str) and phrase != "---autotargeting" else None
            current_bid = self._auction_forecast_strict_int(raw_keyword.get("Bid"))
            item.current_search_bid_rub = (
                current_bid / 1_000_000 if current_bid is not None else None
            )
            if source_campaign_id != expected_campaign_id:
                item.status = "FAILED"
                item.reason = "KEYWORD_NOT_IN_CAMPAIGN"
            elif phrase == "---autotargeting":
                item.status = "NOT_APPLICABLE"
                item.reason = "AUTOTARGETING"
            elif not isinstance(phrase, str):
                item.status = "FAILED"
                item.reason = "MALFORMED_KEYWORD_RESULT"
            elif not manual_strategy_supported:
                item.status = "NOT_APPLICABLE"
                item.reason = "INCOMPATIBLE_CAMPAIGN_STRATEGY"
            elif (
                raw_keyword.get("State") != "ON"
                or raw_keyword.get("Status") != "ACCEPTED"
                or raw_keyword.get("ServingStatus") != "ELIGIBLE"
            ):
                item.status = "NOT_APPLICABLE"
                item.reason = "KEYWORD_NOT_ELIGIBLE"
            else:
                eligible_keyword_ids.append(keyword_id)

        auction_units: list[int] = []
        target_bid_micros_by_keyword_id: dict[int, int] = {}
        for start in range(0, len(eligible_keyword_ids), _AUCTION_FORECAST_BATCH_SIZE):
            batch = eligible_keyword_ids[start : start + _AUCTION_FORECAST_BATCH_SIZE]
            batch_ids = set(batch)
            try:
                auction_response = client.keywordbids_get(
                    campaign_id,
                    keyword_ids=batch,
                    limit=_AUCTION_FORECAST_BATCH_SIZE,
                    offset=0,
                    include_auction_bids=True,
                    include_coverage=False,
                )
            except YandexDirectError:
                for keyword_id in batch:
                    items_by_keyword_id[keyword_id].status = "FAILED"
                    items_by_keyword_id[keyword_id].reason = "UPSTREAM_AUCTION_READ_FAILED"
                continue
            if not auction_response.get("ok"):
                for keyword_id in batch:
                    items_by_keyword_id[keyword_id].status = "FAILED"
                    items_by_keyword_id[keyword_id].reason = "UPSTREAM_AUCTION_READ_FAILED"
                continue
            raw_units = auction_response.get("units")
            units = _try_int(raw_units) if raw_units is not None else None
            if units is not None:
                auction_units.append(units)
            auction_result = auction_response.get("result")
            raw_rows = auction_result.get("KeywordBids") if isinstance(auction_result, dict) else None
            if not isinstance(raw_rows, list):
                for keyword_id in batch:
                    items_by_keyword_id[keyword_id].status = "FAILED"
                    items_by_keyword_id[keyword_id].reason = "MALFORMED_AUCTION_RESULT"
                continue

            rows_by_keyword_id: dict[int, dict[str, Any]] = {}
            duplicate_row_ids: set[int] = set()
            for raw_row in raw_rows:
                if not isinstance(raw_row, dict):
                    continue
                keyword_id = self._auction_forecast_strict_int(raw_row.get("KeywordId"))
                if keyword_id not in batch_ids:
                    continue
                if keyword_id in rows_by_keyword_id:
                    duplicate_row_ids.add(keyword_id)
                    continue
                rows_by_keyword_id[keyword_id] = raw_row

            for keyword_id in batch:
                item = items_by_keyword_id[keyword_id]
                raw_row = rows_by_keyword_id.get(keyword_id)
                if keyword_id in duplicate_row_ids or raw_row is None:
                    item.status = "FAILED"
                    item.reason = "MALFORMED_AUCTION_RESULT"
                    continue
                if self._auction_forecast_strict_int(raw_row.get("CampaignId")) != expected_campaign_id:
                    item.status = "FAILED"
                    item.reason = "MALFORMED_AUCTION_RESULT"
                    continue
                raw_search = raw_row.get("Search")
                if not isinstance(raw_search, dict):
                    item.status = "FAILED"
                    item.reason = "MALFORMED_AUCTION_RESULT"
                    continue
                current_bid = self._auction_forecast_strict_int(raw_search.get("Bid"))
                raw_auction = raw_search.get("AuctionBids")
                if current_bid is None or current_bid < 0:
                    item.status = "FAILED"
                    item.reason = "MALFORMED_AUCTION_RESULT"
                    continue
                # ``AuctionBids: null`` is a documented unavailable shape, not
                # a license to infer a lower level or reuse the current bid.
                if raw_auction is None:
                    item.status = "UNAVAILABLE"
                    item.reason = "TARGET_LEVEL_NOT_AVAILABLE"
                    continue
                raw_auction_items = (
                    raw_auction.get("AuctionBidItems") if isinstance(raw_auction, dict) else None
                )
                if not isinstance(raw_auction_items, list):
                    item.status = "FAILED"
                    item.reason = "MALFORMED_AUCTION_RESULT"
                    continue

                selected_bid_micros: int | None = None
                selected_price_micros: int | None = None
                seen_traffic_levels: set[int] = set()
                malformed = False
                for raw_auction_item in raw_auction_items:
                    if not isinstance(raw_auction_item, dict):
                        malformed = True
                        break
                    traffic_volume = self._auction_forecast_strict_int(
                        raw_auction_item.get("TrafficVolume")
                    )
                    bid_micros = self._auction_forecast_strict_int(raw_auction_item.get("Bid"))
                    price_micros = self._auction_forecast_strict_int(raw_auction_item.get("Price"))
                    if (
                        traffic_volume is None
                        or bid_micros is None
                        or price_micros is None
                        or traffic_volume < 0
                        or bid_micros < 0
                        or price_micros < 0
                        or traffic_volume in seen_traffic_levels
                    ):
                        malformed = True
                        break
                    seen_traffic_levels.add(traffic_volume)
                    if traffic_volume == payload.target_traffic_volume:
                        selected_bid_micros = bid_micros
                        selected_price_micros = price_micros
                if malformed:
                    item.status = "FAILED"
                    item.reason = "MALFORMED_AUCTION_RESULT"
                    continue
                item.current_search_bid_rub = current_bid / 1_000_000
                if selected_bid_micros is None or selected_price_micros is None:
                    item.status = "UNAVAILABLE"
                    item.reason = "TARGET_LEVEL_NOT_AVAILABLE"
                    continue
                item.target_bid_rub = selected_bid_micros / 1_000_000
                item.target_price_rub = selected_price_micros / 1_000_000
                item.status = "READY"
                item.reason = None
                target_bid_micros_by_keyword_id[keyword_id] = selected_bid_micros

        writer_items = [
            {"KeywordId": keyword_id, "SearchBid": target_bid_micros_by_keyword_id[keyword_id]}
            for keyword_id in payload.keyword_ids
            if items_by_keyword_id[keyword_id].status == "READY"
        ]
        payload_preview = {"method": "set", "params": {"KeywordBids": writer_items}}

        def build_result(
            *,
            applied: bool,
            audit_event: str,
            readback: list[dict] | None = None,
            provider_warnings: list[ProviderWarning] | None = None,
            set_results: list[KeywordBidSetItemResult] | None = None,
            partial_failure: bool = False,
            yandex_units: int | None = None,
            yandex_error: str | None = None,
        ) -> KeywordBidTrafficLevelResult:
            audit = self.append_audit(
                audit_event,
                campaign_id,
                dry_run=payload.dry_run,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "target_traffic_volume": payload.target_traffic_volume,
                    "keyword_ids": payload.keyword_ids,
                    "ready_count": len(writer_items),
                    "source": "yandex",
                    "mode": settings.directpilot_mode,
                    "applied": applied,
                    "partial_failure": partial_failure,
                },
            )
            return KeywordBidTrafficLevelResult(
                campaign_id=campaign_id,
                target_traffic_volume=payload.target_traffic_volume,
                mode=settings.directpilot_mode,
                dry_run=payload.dry_run,
                applied=applied,
                audit_id=audit.id,
                items=[items_by_keyword_id[keyword_id] for keyword_id in payload.keyword_ids],
                payload_preview=payload_preview,
                readback=readback,
                provider_warnings=provider_warnings or [],
                set_results=set_results,
                partial_failure=partial_failure,
                yandex_units=yandex_units,
                yandex_error=yandex_error,
            )

        if payload.dry_run:
            result = build_result(
                applied=False,
                audit_event="yandex_keyword_bid_traffic_level_previewed",
                yandex_units=sum(auction_units) if auction_units else None,
            )
            self._keyword_bid_traffic_level_results_by_key[cache_key] = {
                "result": result,
                "request_fingerprint": fingerprint,
            }
            return result

        if not writer_items:
            result = build_result(
                applied=False,
                audit_event="yandex_keyword_bid_traffic_level_not_applied",
                yandex_units=sum(auction_units) if auction_units else None,
            )
            self._keyword_bid_traffic_level_results_by_key[cache_key] = {
                "result": result,
                "request_fingerprint": fingerprint,
            }
            return result

        try:
            set_response = client.keywordbids_set(writer_items)
        except YandexDirectError:
            raise
        except Exception as exc:
            raise YandexDirectError(
                f"keywordbids.set failed: {type(exc).__name__}"
            ) from exc
        if not set_response.get("ok"):
            raise YandexDirectError("keywordbids.set failed")

        provider_warnings = _provider_warnings_from_result(set_response)
        set_results, set_error_summary = _extract_set_results(set_response.get("result"))
        expected_writer_ids = {item["KeywordId"] for item in writer_items}
        result_ids = [item.keyword_id for item in set_results or []]
        malformed_set_results = (
            set_results is None
            or len(result_ids) != len(expected_writer_ids)
            or set(result_ids) != expected_writer_ids
            or len(set(result_ids)) != len(result_ids)
        )
        if malformed_set_results:
            for keyword_id in expected_writer_ids:
                item = items_by_keyword_id[keyword_id]
                item.status = "FAILED"
                item.reason = "MALFORMED_SET_RESULT"
        else:
            assert set_results is not None
            for set_result in set_results:
                if set_result.has_errors:
                    item = items_by_keyword_id[set_result.keyword_id]
                    item.status = "FAILED"
                    item.reason = "PROVIDER_SET_ERROR"
                elif set_result.has_warnings:
                    provider_warnings.extend(
                        warning
                        for warning in set_result.warnings
                        if not any(
                            existing.code == warning.code and existing.message == warning.message
                            for existing in provider_warnings
                        )
                    )

        successful_ids = [
            item["KeywordId"]
            for item in writer_items
            if items_by_keyword_id[item["KeywordId"]].status == "READY"
        ]
        readback: list[dict] = []
        readback_failed = False
        if successful_ids:
            try:
                readback_response = client.keywords_get(campaign_id, keyword_ids=successful_ids)
            except YandexDirectError:
                readback_response = {"ok": False}
            if not readback_response.get("ok"):
                readback_failed = True
            else:
                readback_result = readback_response.get("result")
                raw_readback_rows = (
                    readback_result.get("Keywords") if isinstance(readback_result, dict) else None
                )
                if not isinstance(raw_readback_rows, list):
                    readback_failed = True
                else:
                    rows_by_keyword_id: dict[int, dict[str, Any]] = {}
                    duplicate_readback_ids: set[int] = set()
                    for raw_row in raw_readback_rows:
                        if not isinstance(raw_row, dict):
                            continue
                        keyword_id = self._auction_forecast_strict_int(raw_row.get("Id"))
                        if keyword_id not in successful_ids:
                            continue
                        if keyword_id in rows_by_keyword_id:
                            duplicate_readback_ids.add(keyword_id)
                            continue
                        rows_by_keyword_id[keyword_id] = raw_row
                    for keyword_id in successful_ids:
                        raw_row = rows_by_keyword_id.get(keyword_id)
                        bid_micros = (
                            self._auction_forecast_strict_int(raw_row.get("Bid"))
                            if isinstance(raw_row, dict)
                            else None
                        )
                        if (
                            keyword_id in duplicate_readback_ids
                            or not isinstance(raw_row, dict)
                            or self._auction_forecast_strict_int(raw_row.get("CampaignId"))
                            != expected_campaign_id
                            or bid_micros != target_bid_micros_by_keyword_id[keyword_id]
                        ):
                            items_by_keyword_id[keyword_id].status = "FAILED"
                            items_by_keyword_id[keyword_id].reason = "READBACK_MISMATCH"
                            readback_failed = True
                            continue
                        items_by_keyword_id[keyword_id].status = "APPLIED"
                        items_by_keyword_id[keyword_id].reason = None
                        assert bid_micros is not None
                        readback.append(
                            {
                                "keyword_id": keyword_id,
                                "search_bid_micros": bid_micros,
                                "search_bid_rub": bid_micros / 1_000_000,
                            }
                        )
        if readback_failed:
            for keyword_id in successful_ids:
                if items_by_keyword_id[keyword_id].status == "READY":
                    items_by_keyword_id[keyword_id].status = "FAILED"
                    items_by_keyword_id[keyword_id].reason = "READBACK_UNAVAILABLE"

        partial_failure = (
            malformed_set_results
            or set_error_summary is not None
            or readback_failed
            or any(
                items_by_keyword_id[keyword_id].status == "FAILED"
                for keyword_id in expected_writer_ids
            )
        )
        applied = bool(expected_writer_ids) and not partial_failure and all(
            items_by_keyword_id[keyword_id].status == "APPLIED"
            for keyword_id in expected_writer_ids
        )
        raw_set_units = set_response.get("units")
        set_units = _try_int(raw_set_units) if raw_set_units is not None else None
        if set_units is None:
            set_units = 0
        result = build_result(
            applied=applied,
            audit_event=(
                "yandex_keyword_bid_traffic_level_applied"
                if applied
                else "yandex_keyword_bid_traffic_level_failed"
            ),
            readback=readback or None,
            provider_warnings=provider_warnings,
            set_results=set_results,
            partial_failure=partial_failure,
            yandex_units=sum(auction_units) + set_units,
            yandex_error=(
                None
                if applied
                else "One or more keyword bids were not confirmed by the Direct readback"
            ),
        )
        self._keyword_bid_traffic_level_results_by_key[cache_key] = {
            "result": result,
            "request_fingerprint": fingerprint,
        }
        return result

    # --------------------------------------------------- keyword bids update
    #
    # ``POST /yandex/campaigns/{campaign_id}/bids`` updates SearchBid /
    # ContextBid for existing keywords via v5 ``keywordbids.set``.
    # The gate contract is identical to the rest of the product:
    # ``dry_run=True`` is preview-only; real apply requires
    # ``live_write``, ``approved=True``, ``idempotency_key``, and
    # ``dry_run=False``.
    #
    # ``search_bid_rub`` / ``context_bid_rub`` are received in RUBLES
    # (public REST convention) and converted to Direct micros
    # (multiply by 1_000_000) before building the v5 payload.
    #
    # The minimal v5 item shape for known keyword ids is
    # ``KeywordId + SearchBid`` / ``KeywordId + ContextBid`` — no
    # CampaignId / AdGroupId in the item (Direct returns error_code=9300
    # for that form on batch updates).
    #
    # After apply, read back keyword bids for the campaign and return
    # the changed keyword ids with current Bid/ContextBid.

    def _classify_explicit_autotargeting_keyword_ids(
        self,
        campaign_id: str,
        keyword_ids: list[int],
        *,
        client: YandexDirectClient,
    ) -> dict[int, bool]:
        """Return verified autotargeting classification for explicit field use."""
        response = client.keywords_get(campaign_id, keyword_ids=keyword_ids)
        if not response.get("ok"):
            raise YandexDirectError(
                "keywords.get failed before autotargeting bid-field classification"
            )
        result = response.get("result")
        raw_keywords = result.get("Keywords") if isinstance(result, dict) else None
        if not isinstance(raw_keywords, list):
            raise YandexDirectError(
                "keywords.get did not provide a safe autotargeting classification"
            )

        requested_ids = set(keyword_ids)
        classifications: dict[int, bool] = {}
        duplicate_ids: set[int] = set()
        for raw_keyword in raw_keywords:
            if not isinstance(raw_keyword, dict):
                continue
            keyword_id = self._auction_forecast_strict_int(raw_keyword.get("Id"))
            if keyword_id not in requested_ids:
                continue
            if keyword_id in classifications:
                duplicate_ids.add(keyword_id)
                continue
            source_campaign_id = raw_keyword.get("CampaignId")
            if source_campaign_id is not None and str(source_campaign_id) != campaign_id:
                raise YandexDirectError(
                    "keywords.get returned a keyword outside the requested campaign"
                )
            phrase = raw_keyword.get("Keyword")
            if not isinstance(phrase, str):
                continue
            classifications[keyword_id] = phrase == "---autotargeting"

        if duplicate_ids or set(classifications) != requested_ids:
            raise YandexDirectError(
                "keyword type could not be determined safely before keywordbids.set"
            )
        return classifications

    def yandex_keyword_bids_update(
        self,
        campaign_id: str,
        payload: "KeywordBidUpdateRequest",
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> "KeywordBidUpdateResult":
        """Apply keyword bid changes for an existing campaign."""

        if not payload.approved:
            raise ValueError("Action requires explicit approval")

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"
        if not payload.dry_run and not can_write:
            raise YandexDirectError(
                "Live writes are not allowed in live_readonly mode; use live_write"
            )

        # --- Idempotency cache check ----------------------------------------
        cache_key = f"keyword_bids:{campaign_id}:{payload.idempotency_key}"

        # --- Build v5 payload -----------------------------------------------
        explicit_autotargeting_keyword_ids = [
            item.keyword_id
            for item in payload.items
            if item.autotargeting_search_bid_is_auto is not None
        ]
        if explicit_autotargeting_keyword_ids:
            if not is_live or client is None:
                raise YandexDirectError(
                    "A Yandex Direct client is required to verify autotargeting "
                    "keyword type before using autotargeting_search_bid_is_auto"
                )
            is_autotargeting_by_keyword_id = (
                self._classify_explicit_autotargeting_keyword_ids(
                    campaign_id,
                    explicit_autotargeting_keyword_ids,
                    client=client,
                )
            )
            v5_items: list[dict[str, Any]] = [
                item.to_direct_micros_item(
                    is_autotargeting=(
                        is_autotargeting_by_keyword_id[item.keyword_id]
                        if item.autotargeting_search_bid_is_auto is not None
                        else None
                    )
                )
                for item in payload.items
            ]
        else:
            v5_items = [item.to_direct_micros_item() for item in payload.items]
        payload_preview: dict = {"method": "set", "params": {"KeywordBids": v5_items}}
        payload_fingerprint = _keyword_bids_request_fingerprint(v5_items)

        # --- Idempotency cache check ----------------------------------------
        if cache_key in self._keyword_bids_results_by_key:
            cache_record = self._keyword_bids_results_by_key[cache_key]
            cached = cache_record["result"]
            cached_fingerprint = cache_record.get("request_fingerprint")

            if cached.dry_run != payload.dry_run:
                raise ValueError(
                    f"Idempotency key {payload.idempotency_key!r} was "
                    f"previously used with dry_run={cached.dry_run}; "
                    f"replay with dry_run={payload.dry_run} is not allowed"
                )
            if cached_fingerprint != payload_fingerprint:
                raise ValueError(
                    f"Idempotency key {payload.idempotency_key!r} was "
                    "previously used with a different keyword bids payload; "
                    "replay is rejected"
                )
            return cached

        # --- Mock mode ------------------------------------------------------
        if not is_live:
            audit = self.append_audit(
                "yandex_keyword_bids_requested",
                campaign_id,
                dry_run=payload.dry_run,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "item_count": len(payload.items),
                    "source": "mock",
                },
            )
            result = KeywordBidUpdateResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=payload.dry_run,
                applied=False,
                source="mock",
                audit_id=audit.id,
                payload_preview=payload_preview if payload.dry_run else None,
            )
            self._keyword_bids_results_by_key[cache_key] = {
                "result": result,
                "request_fingerprint": payload_fingerprint,
            }
            return result

        # --- Dry-run in live modes ------------------------------------------
        if payload.dry_run:
            audit = self.append_audit(
                "yandex_keyword_bids_requested",
                campaign_id,
                dry_run=True,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "item_count": len(payload.items),
                    "source": "yandex",
                    "mode": mode,
                },
            )
            result = KeywordBidUpdateResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=True,
                applied=False,
                source="yandex",
                audit_id=audit.id,
                payload_preview=payload_preview,
            )
            self._keyword_bids_results_by_key[cache_key] = {
                "result": result,
                "request_fingerprint": payload_fingerprint,
            }
            return result

        # --- Write gate -----------------------------------------------------
        if client is None:
            raise YandexDirectError(
                "Yandex Direct client is required for live keyword bids updates"
            )

        # --- Real apply -----------------------------------------------------
        try:
            response = client.keywordbids_set(v5_items)
        except YandexDirectError:
            raise
        except Exception as exc:
            raise YandexDirectError(
                f"keywordbids.set failed: {type(exc).__name__}: {exc}"
            ) from exc

        # Extract top-level warnings (v5 ``Warnings[]``)
        provider_warnings = _provider_warnings_from_result(response)

        top_level_ok = response.get("ok", False)
        yandex_error: str | None = None
        if not top_level_ok:
            error_block = response.get("error") or {}
            error_code = error_block.get("error_code") or error_block.get("Code")
            error_detail = (
                error_block.get("error_detail")
                or error_block.get("error_string")
                or error_block.get("Message")
                or "keywordbids.set failed"
            )
            raise YandexDirectError(
                f"keywordbids.set failed: {error_code or 'unknown'}",
                diagnostics={
                    "error_code": error_code,
                    "error_detail": str(error_detail)[:500],
                    "payload_preview": payload_preview,
                },
            )

        # --- Per-item SetResults inspection ---------------------------------
        v5_result = response.get("result") if isinstance(response, dict) else None
        set_results, set_error_summary = _extract_set_results(v5_result)
        expected_writer_ids = {item["KeywordId"] for item in v5_items}
        result_ids = [item.keyword_id for item in set_results or []]
        malformed_set_results = (
            set_results is None
            or len(result_ids) != len(expected_writer_ids)
            or set(result_ids) != expected_writer_ids
            or len(set(result_ids)) != len(result_ids)
        )

        partial_failure = False
        if malformed_set_results:
            partial_failure = True
            yandex_error = "malformed SetResults"
        if set_error_summary:
            # At least one item has Errors — the apply is not fully successful.
            partial_failure = True
            # Surface the item-level error summary alongside any top-level
            # error so the operator sees both levels.
            if yandex_error:
                yandex_error = f"{yandex_error}; item-level: {set_error_summary}"
            else:
                yandex_error = f"item-level: {set_error_summary}"

        # Collect item-level warnings into provider_warnings
        if set_results:
            for item in set_results:
                if item.has_warnings:
                    for w in item.warnings:
                        # Avoid duplicating warnings already surfaced at top level
                        already_present = any(
                            pw.code == w.code and pw.message == w.message
                            for pw in provider_warnings
                        )
                        if not already_present:
                            provider_warnings.append(w)

        # --- Readback -------------------------------------------------------
        changed_keyword_ids = [item.keyword_id for item in payload.items]
        readback: list[dict] | None = None
        # Only attempt readback after a fully validated provider result.
        if top_level_ok and not partial_failure and client is not None:
            try:
                # Use keywords_get to read back current bids
                keywords_response = client.keywords_get(campaign_id)
                if keywords_response.get("ok"):
                    kw_result = keywords_response.get("result") or {}
                    if isinstance(kw_result, dict):
                        all_keywords = kw_result.get("Keywords") or []
                        readback = [
                            {
                                "KeywordId": kw["Id"],
                                "Bid": kw.get("Bid"),
                                "ContextBid": kw.get("ContextBid"),
                            }
                            for kw in all_keywords
                            if isinstance(kw, dict) and kw.get("Id") in changed_keyword_ids
                        ]
            except Exception:
                readback = None  # readback is best-effort

        # Determine overall applied flag:
        # applied=True only when top-level ok AND no item-level errors
        applied = top_level_ok and not partial_failure

        audit = self.append_audit(
            "yandex_keyword_bids_applied",
            campaign_id,
            dry_run=False,
            details={
                "approved": payload.approved,
                "idempotency_key": payload.idempotency_key,
                "reason": payload.reason,
                "item_count": len(payload.items),
                "keyword_ids": changed_keyword_ids,
                "source": "yandex",
                "mode": mode,
                "ok": top_level_ok,
                "partial_failure": partial_failure,
                "set_error_summary": set_error_summary,
                "yandex_error": yandex_error,
            },
        )

        result = KeywordBidUpdateResult(
            campaign_id=campaign_id,
            mode=mode,
            dry_run=False,
            applied=applied,
            source="yandex",
            audit_id=audit.id,
            readback=readback,
            provider_warnings=provider_warnings,
            set_results=set_results,
            partial_failure=partial_failure,
            yandex_units=_try_int(response.get("units")) if response.get("units") is not None else None,
            yandex_error=yandex_error,
        )
        self._keyword_bids_results_by_key[cache_key] = {
            "result": result,
            "request_fingerprint": payload_fingerprint,
        }
        return result

    def yandex_bid_modifiers_update(
        self,
        campaign_id: str,
        payload: "BidModifiersUpdateRequest",
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> "BidModifiersUpdateResult":
        """Preview/apply existing Yandex Direct bid modifier coefficient changes."""

        if not payload.approved:
            raise ValueError("Action requires explicit approval")
        if not payload.idempotency_key:
            raise ValueError("idempotency_key is required for bid modifiers update")

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"

        preview_payload = payload.build_payload_preview(campaign_id)
        fingerprint_payload = (
            payload.build_direct_set_payload() if not payload.dry_run else preview_payload
        )
        payload_fingerprint = _bid_modifiers_request_fingerprint(fingerprint_payload)
        cache_key = f"bid_modifiers:{campaign_id}:{payload.idempotency_key}"

        if cache_key in self._bid_modifiers_results_by_key:
            cache_record = self._bid_modifiers_results_by_key[cache_key]
            cached = cache_record["result"]
            cached_fingerprint = cache_record.get("request_fingerprint")
            if cached.dry_run != payload.dry_run:
                raise ValueError(
                    f"Idempotency key {payload.idempotency_key!r} was previously "
                    f"used with dry_run={cached.dry_run}; replay with "
                    f"dry_run={payload.dry_run} is not allowed"
                )
            if cached_fingerprint != payload_fingerprint:
                raise ValueError(
                    f"Idempotency key {payload.idempotency_key!r} was previously "
                    "used with a different bid modifiers payload; replay is rejected"
                )
            return cached

        if not is_live:
            audit = self.append_audit(
                "yandex_bid_modifiers_requested",
                campaign_id,
                dry_run=payload.dry_run,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "item_count": len(payload.adjustments),
                    "source": "mock",
                },
            )
            result = BidModifiersUpdateResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=payload.dry_run,
                applied=False,
                source="mock",
                audit_id=audit.id,
                payload_preview=preview_payload if payload.dry_run else None,
            )
            self._bid_modifiers_results_by_key[cache_key] = {
                "result": result,
                "request_fingerprint": payload_fingerprint,
            }
            return result

        if payload.dry_run:
            audit = self.append_audit(
                "yandex_bid_modifiers_requested",
                campaign_id,
                dry_run=True,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "item_count": len(payload.adjustments),
                    "source": "yandex",
                    "mode": mode,
                },
            )
            result = BidModifiersUpdateResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=True,
                applied=False,
                source="yandex",
                audit_id=audit.id,
                payload_preview=preview_payload,
            )
            self._bid_modifiers_results_by_key[cache_key] = {
                "result": result,
                "request_fingerprint": payload_fingerprint,
            }
            return result

        if not can_write:
            raise YandexDirectError(
                "Live writes are not allowed in live_readonly mode; use live_write"
            )
        if client is None:
            raise YandexDirectError(
                "Yandex Direct client is required for live bid modifiers updates"
            )

        direct_payload = payload.build_direct_set_payload()
        v5_items = direct_payload["BidModifiers"]
        call_payload_preview = {"method": "set", "params": direct_payload}
        try:
            response = client.bidmodifiers_set(v5_items)
        except YandexDirectError:
            raise
        except Exception as exc:
            raise YandexDirectError(
                f"bidmodifiers.set failed: {type(exc).__name__}: {exc}"
            ) from exc

        provider_warnings = _provider_warnings_from_result(response)
        top_level_ok = response.get("ok", False)
        if not top_level_ok:
            error_block = response.get("error") or {}
            error_code = error_block.get("error_code") or error_block.get("Code")
            error_detail = (
                error_block.get("error_detail")
                or error_block.get("error_string")
                or error_block.get("Message")
                or "bidmodifiers.set failed"
            )
            raise YandexDirectError(
                f"bidmodifiers.set failed: {error_code or 'unknown'}",
                diagnostics={
                    "error_code": error_code,
                    "error_detail": str(error_detail)[:500],
                    "payload_preview": call_payload_preview,
                },
            )

        set_results, item_error_summary = _extract_bid_modifier_set_results(
            response.get("result")
        )
        partial_failure = item_error_summary is not None

        readback: list[dict] | None = None
        try:
            readback_response = client.bidmodifiers_get(campaign_id)
            if readback_response.get("ok"):
                result_block = readback_response.get("result") or {}
                if isinstance(result_block, dict):
                    raw_items = result_block.get("BidModifiers") or result_block.get("Items") or []
                    readback = [item for item in raw_items if isinstance(item, dict)]
        except Exception:
            readback = None

        audit = self.append_audit(
            "yandex_bid_modifiers_applied",
            campaign_id,
            dry_run=False,
            details={
                "approved": payload.approved,
                "idempotency_key": payload.idempotency_key,
                "reason": payload.reason,
                "item_count": len(payload.adjustments),
                "source": "yandex",
                "mode": mode,
                "ok": top_level_ok,
                "partial_failure": partial_failure,
                "item_error_summary": item_error_summary,
            },
        )
        response_units = response.get("units")
        result = BidModifiersUpdateResult(
            campaign_id=campaign_id,
            mode=mode,
            dry_run=False,
            applied=not partial_failure,
            source="yandex",
            audit_id=audit.id,
            readback=readback,
            provider_warnings=provider_warnings,
            set_results=set_results,
            partial_failure=partial_failure,
            yandex_units=_try_int(response_units) if response_units is not None else None,
            yandex_error=item_error_summary,
        )
        self._bid_modifiers_results_by_key[cache_key] = {
            "result": result,
            "request_fingerprint": payload_fingerprint,
        }
        return result


def _try_int(value: str | int) -> int | None:
    """Try to parse a value as int; return None on failure."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _redact_ads_payload(ads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a redacted preview of the ads payload (no raw tokens)."""
    return [
        {
            k: v
            for k, v in ad.items()
        }
        for ad in ads
    ]


store = MockStore()
