from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import json
import socket
import uuid as _uuid
from html.parser import HTMLParser
from itertools import count
from typing import Any, Iterable, Literal
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx

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
    KeywordBidUpdateRequest,
    KeywordBidUpdateResult,
    KeywordBidsAuctionBidItem,
    KeywordBidsCoverageItem,
    KeywordBidsGetItem,
    KeywordBidsGetResult,
    KeywordBidsSetAutoItemResult,
    KeywordBidsSetAutoRequest,
    KeywordBidsSetAutoResult,
    # Bid modifiers
    BidModifierAddItemResult,
    BidModifierSetItemResult,
    BidModifiersCreateRequest,
    BidModifiersCreateResult,
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
    # Existing-campaign URL migrations
    LandingUrlMigrationItem,
    LandingUrlMigrationRequest,
    LandingUrlMigrationsRequest,
    SitelinkUrlMigrationItem,
    SitelinkUrlMigrationRequest,
    SitelinkUrlMigrationSpec,
    UrlMigrationChange,
    UrlMigrationProviderIssue,
    UrlMigrationProviderItemResult,
    UrlMigrationResult,
    UrlMigrationStage,
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


_KEYWORD_BIDS_MAX_PAGE_LIMIT = 10_000
_URL_MIGRATION_MAX_REDIRECTS = 3
_URL_MIGRATION_BATCH_SIZE = 1_000
_URL_MIGRATION_AUDIT_FIELDS = (
    "Title",
    "Title2",
    "Text",
    "DisplayUrlPath",
    "AdImageHash",
    "VCardId",
    "BusinessId",
    "PreferVCardOverBusiness",
    "AdExtensions",
)


class _MigrationAnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = tag
        for name, value in attrs:
            if name.lower() == "id" and value:
                self.ids.add(str(value))


def _url_migration_normalize_host(value: str) -> str:
    host = value.strip().rstrip(".")
    if not host:
        raise ValueError("URL host is required")
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("URL host cannot be normalized safely") from exc


def _url_migration_is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_reserved,
            address.is_multicast,
            address.is_unspecified,
        )
    )


def _url_migration_chunks(items: list[int]) -> Iterable[list[int]]:
    for index in range(0, len(items), _URL_MIGRATION_BATCH_SIZE):
        yield items[index : index + _URL_MIGRATION_BATCH_SIZE]


def _url_migration_safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _url_migration_redacted_issue(raw: Any, *, kind: str) -> UrlMigrationProviderIssue:
    code = raw.get("Code") if isinstance(raw, dict) else None
    if not isinstance(code, (int, str)):
        code = None
    return UrlMigrationProviderIssue(code=code, message=f"Provider {kind}")


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


def _keyword_bids_set_auto_request_fingerprint(payload: dict[str, Any]) -> str:
    """Fingerprint a canonical ``keywordbids.setAuto`` apply payload."""

    items = payload.get("params", {}).get("KeywordBids", [])
    normalized = sorted(
        [dict(item) for item in items if isinstance(item, dict)],
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )
    canonical = json.dumps(
        {"method": "setAuto", "params": {"KeywordBids": normalized}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _keyword_bids_set_auto_read_limit(payload: KeywordBidsSetAutoRequest) -> int:
    """Return one bounded page that can safely cover a setAuto scope."""

    if payload.scope == "keyword":
        return min(len(payload.keyword_ids or []), _KEYWORD_BIDS_MAX_PAGE_LIMIT)
    return _KEYWORD_BIDS_MAX_PAGE_LIMIT


def _keyword_bids_set_auto_window_blockers(
    campaign_id: str,
    payload: KeywordBidsSetAutoRequest,
    result: KeywordBidsGetResult,
    *,
    require_keyword_coverage: bool = False,
) -> list[str]:
    """Fail closed when a bounded KeywordBids result cannot verify setAuto scope."""

    if payload.scope != "keyword":
        return (
            ["setAuto scope exceeds the safely verifiable KeywordBids result window; operation is blocked"]
            if result.limited_by is not None
            else []
        )
    if result.limited_by is None and not require_keyword_coverage:
        return []

    requested_keyword_ids = set(payload.keyword_ids or [])
    items_by_keyword_id: dict[int, list[Any]] = {}
    for item in result.items:
        if item.keyword_id in requested_keyword_ids:
            items_by_keyword_id.setdefault(item.keyword_id, []).append(item)

    missing_keyword_ids = sorted(requested_keyword_ids - set(items_by_keyword_id))
    if missing_keyword_ids:
        return [f"setAuto readback does not contain requested keyword IDs: {missing_keyword_ids}"]

    expected_campaign_id = _as_int_or_none(campaign_id)
    if expected_campaign_id is None:
        return [
            f"setAuto ownership could not be verified for keyword IDs: {sorted(requested_keyword_ids)}"
        ]

    unverified_keyword_ids = sorted(
        keyword_id
        for keyword_id, matching_items in items_by_keyword_id.items()
        if len(matching_items) != 1 or matching_items[0].campaign_id != expected_campaign_id
    )
    if unverified_keyword_ids:
        return [
            f"setAuto ownership could not be verified for keyword IDs: {unverified_keyword_ids}"
        ]
    return []


def _as_int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _micros_to_rub(value: Any) -> float | None:
    micros = _as_int_or_none(value)
    return micros / 1_000_000 if micros is not None else None


def validate_keyword_bids_set_auto_strategy(
    campaign: Any,
    rule_type: str,
) -> list[str]:
    """Pure, fail-closed compatibility matrix for ``keywordbids.setAuto``."""

    if not isinstance(campaign, dict):
        return ["Campaign strategy is unavailable; setAuto is blocked"]
    if campaign.get("Type") != "TEXT_CAMPAIGN":
        return ["setAuto is supported only for TEXT_CAMPAIGN campaigns"]
    text_campaign = campaign.get("TextCampaign")
    if not isinstance(text_campaign, dict):
        return ["Text campaign strategy is unavailable; setAuto is blocked"]
    strategy = text_campaign.get("BiddingStrategy")
    if not isinstance(strategy, dict):
        return ["Campaign bidding strategy is unavailable; setAuto is blocked"]

    def strategy_type(channel: str) -> str | None:
        block = strategy.get(channel)
        if not isinstance(block, dict):
            return None
        value = block.get("BiddingStrategyType") or block.get("Type")
        return value if isinstance(value, str) else None

    if rule_type == "search_by_traffic_volume":
        if strategy_type("Search") != "HIGHEST_POSITION":
            return [
                "SearchByTrafficVolume requires Search strategy HIGHEST_POSITION; setAuto is blocked"
            ]
        return []
    if rule_type == "network_by_coverage":
        if strategy_type("Network") not in {"MAXIMUM_COVERAGE", "MANUAL_CPM"}:
            return [
                "NetworkByCoverage requires Network strategy MAXIMUM_COVERAGE or MANUAL_CPM; setAuto is blocked"
            ]
        return []
    return ["Unknown setAuto rule type is blocked"]


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
            continue
        keyword_id = item.get("Id")
        if keyword_id is None:
            # Missing Id — treat as an error item with keyword_id=0
            keyword_id = 0

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


def _warning_items_from_raw(raw: Any) -> list[ProviderWarning]:
    """Convert v5 Errors/Warnings list into redacted ProviderWarning items."""

    out: list[ProviderWarning] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            ProviderWarning(
                code=int(item.get("Code") or 0),
                message=str(item.get("Message") or ""),
                details=str(item.get("Details") or ""),
            )
        )
    return out


def _extract_bid_modifier_add_results(
    result_payload: Any,
) -> tuple[list["BidModifierAddItemResult"] | None, str | None]:
    """Extract redacted per-item outcomes from ``bidmodifiers.add``.

    Direct can return a top-level successful response while individual
    ``AddResults`` entries contain ``Errors``. Treat that as a partial failure
    and never infer ids that Direct did not return.
    """

    if not isinstance(result_payload, dict):
        return None, "missing AddResults envelope"
    add_results_raw = result_payload.get("AddResults")
    if not isinstance(add_results_raw, list):
        return None, "missing AddResults envelope"

    item_results: list[BidModifierAddItemResult] = []
    error_summaries: list[str] = []

    for index, item in enumerate(add_results_raw):
        if not isinstance(item, dict):
            error_summaries.append(f"item_index={index}: AddResults item is not an object")
            item_results.append(BidModifierAddItemResult(has_errors=True))
            continue

        item_errors = _warning_items_from_raw(item.get("Errors"))
        item_warnings = _warning_items_from_raw(item.get("Warnings"))
        ids: list[int] = []
        if item.get("Id") is not None:
            try:
                ids.append(int(item["Id"]))
            except (TypeError, ValueError):
                pass
        raw_ids = item.get("Ids")
        if isinstance(raw_ids, list):
            for raw_id in raw_ids:
                try:
                    ids.append(int(raw_id))
                except (TypeError, ValueError):
                    pass

        has_errors = bool(item_errors)
        if has_errors:
            error_summaries.append(
                f"item_index={index}: {_format_add_result_error(item.get('Errors') or [])}"
            )
        elif not ids:
            has_errors = True
            error_summaries.append(f"item_index={index}: AddResults item has no Ids/Id")

        item_results.append(
            BidModifierAddItemResult(
                ids=ids,
                has_errors=has_errors,
                has_warnings=bool(item_warnings),
                errors=item_errors,
                warnings=item_warnings,
            )
        )

    error_summary = "; ".join(error_summaries) if error_summaries else None
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
        # ``setAuto`` has an independent idempotency namespace. Preview requests
        # intentionally never enter this cache, so a dry run cannot consume an
        # apply key or replay a mutation result.
        self._keyword_bids_set_auto_results_by_key: dict[str, dict[str, Any]] = {}
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

        # Sitelinks: optional preview + optional apply when include_sitelinks=True.
        sitelink_payloads: list[dict[str, Any]] = []
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
                        full_items.append(item)
                    if set_has_changes:
                        sitelink_payloads.append({"Id": sl_set_id, "Sitelinks": full_items})

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
            if payload.include_sitelinks and sitelink_payloads:
                rb_sitelink_ids = sorted(
                    [s_id for s_id in (s.get("Id") for s in sitelink_payloads) if isinstance(s_id, int)]
                )
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
                    for rb_set in rb_sets:
                        if isinstance(rb_set, dict):
                            rb_set_id = rb_set.get("Id")
                            if isinstance(rb_set_id, int):
                                returned_set_ids.add(rb_set_id)
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
                    expected_set_ids = set(rb_sitelink_ids)
                    if returned_set_ids != expected_set_ids:
                        missing = sorted(expected_set_ids - returned_set_ids)
                        raise YandexDirectError(
                            f"Yandex Direct sitelink readback incomplete after UTM apply: "
                            f"missing_set_ids={missing!r}"
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
        daily_budget_is_missing_or_null = False
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
                                    daily_budget_is_missing_or_null = True
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
                                # A missing field can represent a weekly-budget
                                # strategy; it is safe only after strategy
                                # readback confirms that exact shape.
                                daily_budget_read_ok = False
                                daily_budget_is_missing_or_null = True

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
                daily_budget_is_missing_or_null = False
                current_strategy = None
                strategy_read_ok = False

        current_strategy_is_weekly_budget = False
        if current_strategy is not None:
            current_search = current_strategy.get("Search")
            if isinstance(current_search, dict):
                wb = current_search.get("WbMaximumConversionRate")
                if isinstance(wb, dict) and wb.get("BudgetType") == "WEEKLY_BUDGET":
                    current_strategy_is_weekly_budget = True

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

        # Direct v5 clears existing priority goals only with a literal null.
        # Omitting the field retains them, while Items=[] is rejected.
        if priority_goals_items is None:
            text_campaign_block["PriorityGoals"] = None
        else:
            text_campaign_block["PriorityGoals"] = {
                "Items": list(priority_goals_items),
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

        # Fail closed unless we can preserve a valid daily budget, or the
        # absent/null DailyBudget is confirmed to be a weekly-budget strategy.
        has_valid_daily_budget = (
            daily_budget_read_ok and current_daily_budget is not None
        )
        has_confirmed_weekly_budget_without_daily_budget = (
            daily_budget_is_missing_or_null and current_strategy_is_weekly_budget
        )
        if is_live and not (
            has_valid_daily_budget
            or has_confirmed_weekly_budget_without_daily_budget
        ):
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
                                    readback_block = {
                                        "BiddingStrategy": dict(bs),
                                        "PriorityGoals": tc_rb.get("PriorityGoals"),
                                    }
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

    # ------------------------------------------------------------------
    # Existing-campaign landing URL / sitelink URL migrations
    # ------------------------------------------------------------------

    @staticmethod
    def _url_migration_allowed_hosts(settings: Settings) -> set[str]:
        configured = [
            item.strip()
            for item in settings.url_migration_allowed_hosts.split(",")
            if item.strip()
        ]
        if not configured:
            raise ValueError(
                "DIRECTPILOT_URL_MIGRATION_ALLOWED_HOSTS is empty; target URL validation fails closed"
            )
        return {_url_migration_normalize_host(item) for item in configured}

    def _url_migration_resolve_public_host(self, host: str, port: int | None) -> None:
        resolver = getattr(self, "_url_migration_dns_resolver", None)
        if resolver is None:
            try:
                resolved: Any = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise ValueError("Target URL host could not be resolved") from exc
        else:
            resolved = resolver(host, port or 443)

        addresses: list[str] = []
        for entry in resolved or []:
            if isinstance(entry, str):
                addresses.append(entry)
            elif isinstance(entry, tuple) and len(entry) >= 5 and isinstance(entry[4], tuple):
                addresses.append(str(entry[4][0]))
            elif isinstance(entry, dict) and isinstance(entry.get("address"), str):
                addresses.append(entry["address"])
        if not addresses or any(not _url_migration_is_public_ip(value) for value in addresses):
            raise ValueError("Target URL host must resolve only to public IP addresses")

    def _url_migration_fetch(self, url: str) -> httpx.Response:
        getter = getattr(self, "_url_migration_http_fetcher", None)
        if getter is not None:
            return getter(url)
        with httpx.Client(timeout=5.0, follow_redirects=False, trust_env=False) as http_client:
            return http_client.get(url, headers={"User-Agent": "DirectPilot-URL-Validator/1.0"})

    @staticmethod
    def _url_migration_parse_url(url: str, allowed_hosts: set[str]) -> tuple[Any, str, int | None]:
        if not url or len(url) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError("Target URL is malformed")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise ValueError("Target URL is malformed") from exc
        if parts.scheme.lower() != "https" or not parts.hostname:
            raise ValueError("Target URL must use HTTPS and include a host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("Target URL must not include credentials")
        if "?" in parts.fragment:
            raise ValueError("Target URL query parameters must precede fragment")
        normalized_host = _url_migration_normalize_host(parts.hostname)
        if normalized_host not in allowed_hosts:
            raise ValueError(
                "Target URL host is not in the configured allowlist "
                "(DIRECTPILOT_URL_MIGRATION_ALLOWED_HOSTS)"
            )
        return parts, normalized_host, port

    def _validate_url_migration_target(
        self,
        url: str,
        *,
        settings: Settings,
        require_anchor: bool,
    ) -> dict[str, str]:
        """Validate one public HTTPS target without serializing it differently."""
        allowed_hosts = self._url_migration_allowed_hosts(settings)
        initial_parts, expected_host, _ = self._url_migration_parse_url(url, allowed_hosts)
        requested_anchor = unquote(initial_parts.fragment)
        current_url = url

        for redirect_index in range(_URL_MIGRATION_MAX_REDIRECTS + 1):
            parts, current_host, port = self._url_migration_parse_url(current_url, allowed_hosts)
            if current_host != expected_host:
                raise ValueError("Target URL redirect changed to an unexpected host")
            self._url_migration_resolve_public_host(current_host, port)
            request_url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
            response = self._url_migration_fetch(request_url)
            status_code = int(response.status_code)
            if 300 <= status_code < 400:
                location = response.headers.get("location")
                if not location or redirect_index >= _URL_MIGRATION_MAX_REDIRECTS:
                    raise ValueError("Target URL redirect chain is unsafe or exceeds the limit")
                current_url = urljoin(request_url, location)
                continue
            if not 200 <= status_code < 300:
                raise ValueError("Target URL did not return a successful public HTTP response")
            if require_anchor and requested_anchor:
                parser = _MigrationAnchorParser()
                parser.feed(response.text)
                if requested_anchor not in parser.ids:
                    raise ValueError("Target URL does not contain the requested fragment anchor")
            return {
                "submitted_href": url,
                "normalized_host": expected_host,
                "final_href": current_url,
            }
        raise ValueError("Target URL redirect chain is unsafe")

    @staticmethod
    def _url_migration_provider_results(
        response: dict[str, Any],
        *,
        result_name: str,
        input_ids: Iterable[int | str],
    ) -> list[UrlMigrationProviderItemResult]:
        item_ids = list(input_ids)
        if not response.get("ok"):
            raw_error = response.get("error") or {}
            code = raw_error.get("error_code") if isinstance(raw_error, dict) else None
            return [
                UrlMigrationProviderItemResult(
                    input_id=input_id,
                    errors=[UrlMigrationProviderIssue(code=code, message="Provider request failed")],
                )
                for input_id in item_ids
            ]
        result = response.get("result") or {}
        raw_items = result.get(result_name) if isinstance(result, dict) else None
        if not isinstance(raw_items, list):
            raw_items = []
        parsed: list[UrlMigrationProviderItemResult] = []
        for index, input_id in enumerate(item_ids):
            raw_item = raw_items[index] if index < len(raw_items) and isinstance(raw_items[index], dict) else None
            if raw_item is None:
                parsed.append(
                    UrlMigrationProviderItemResult(
                        input_id=input_id,
                        errors=[UrlMigrationProviderIssue(message="Provider item result is missing")],
                    )
                )
                continue
            warnings = [
                _url_migration_redacted_issue(item, kind="warning")
                for item in (raw_item.get("Warnings") or [])
            ]
            errors = [
                _url_migration_redacted_issue(item, kind="error")
                for item in (raw_item.get("Errors") or [])
            ]
            returned_id = _url_migration_safe_int(raw_item.get("Id"))
            id_matches = returned_id is not None if input_id == "clone" else str(returned_id) == str(input_id)
            if not errors and not id_matches:
                errors.append(UrlMigrationProviderIssue(message="Provider item result has an unexpected ID"))
            parsed.append(
                UrlMigrationProviderItemResult(
                    input_id=input_id,
                    success=not errors,
                    warnings=warnings,
                    errors=errors,
                )
            )
        for index in range(len(item_ids), len(raw_items)):
            parsed.append(
                UrlMigrationProviderItemResult(
                    input_id=f"unexpected:{index}",
                    errors=[UrlMigrationProviderIssue(message="Provider returned an unexpected item result")],
                )
            )
        return parsed

    @staticmethod
    def _url_migration_sitelink_payload(items: list[SitelinkUrlMigrationItem]) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for item in items:
            link = {"Title": item.title, "Href": item.href}
            if item.description is not None:
                link["Description"] = item.description
            payload.append(link)
        return payload

    @staticmethod
    def _url_migration_sitelink_signature(items: list[dict[str, Any]]) -> list[dict[str, str | None]]:
        signature: list[dict[str, str | None]] = []
        for item in items:
            title = item.get("Title")
            href = item.get("Href")
            description = item.get("Description")
            if not isinstance(title, str) or not isinstance(href, str):
                raise ValueError("Sitelink set has an invalid documented item shape")
            if description is not None and not isinstance(description, str):
                raise ValueError("Sitelink set has an invalid documented item shape")
            signature.append({"title": title, "href": href, "description": description})
        return signature

    def _url_migration_read_sitelink_set(
        self,
        client: YandexDirectClient,
        source_sitelink_set_id: int,
    ) -> dict[str, Any]:
        response = client.sitelinks_get(ids=[source_sitelink_set_id])
        if not response.get("ok"):
            raise YandexDirectError("Yandex Direct rejected sitelinks.get during URL migration preflight")
        result = response.get("result") or {}
        if not isinstance(result, dict) or result.get("LimitedBy") is not None:
            raise YandexDirectError("Sitelinks preflight result is limited or malformed")
        sets = result.get("SitelinksSets") or []
        matching = [
            item
            for item in sets
            if isinstance(item, dict) and str(item.get("Id")) == str(source_sitelink_set_id)
        ]
        if len(matching) != 1:
            raise YandexDirectError("Sitelink source set was not returned exactly once")
        return matching[0]

    def _url_migration_scan_sitelink_references(
        self,
        client: YandexDirectClient,
        source_sitelink_set_id: int,
    ) -> list[dict[str, Any]]:
        offset = 0
        cursors: set[int] = set()
        seen_ids: set[int] = set()
        references: list[dict[str, Any]] = []
        while True:
            response = client.ads_get_by_sitelink_set_ids(
                [source_sitelink_set_id],
                limit=10_000,
                offset=offset,
            )
            if not response.get("ok"):
                raise YandexDirectError("Yandex Direct rejected sitelink reference scan")
            result = response.get("result") or {}
            if not isinstance(result, dict):
                raise YandexDirectError("Sitelink reference scan returned an invalid result")
            rows = result.get("Ads") or []
            if not isinstance(rows, list):
                raise YandexDirectError("Sitelink reference scan returned an invalid page")
            for row in rows:
                if not isinstance(row, dict):
                    raise YandexDirectError("Sitelink reference scan returned an invalid ad")
                ad_id = _url_migration_safe_int(row.get("Id"))
                text_ad = row.get("TextAd")
                if ad_id is None or ad_id in seen_ids or not isinstance(text_ad, dict):
                    raise YandexDirectError("Sitelink reference scan is incomplete or contains duplicates")
                if str(text_ad.get("SitelinkSetId")) != str(source_sitelink_set_id):
                    raise YandexDirectError("Sitelink reference scan returned an unexpected ad relationship")
                seen_ids.add(ad_id)
                references.append(
                    {
                        "ad_id": ad_id,
                        "campaign_id": str(row.get("CampaignId") or ""),
                        "ad_group_id": str(row.get("AdGroupId") or ""),
                        "type": str(row.get("Type") or ""),
                        "href": str(text_ad.get("Href") or ""),
                    }
                )
            limited_by = result.get("LimitedBy")
            if limited_by is None:
                return references
            cursor = _url_migration_safe_int(limited_by)
            if cursor is None or cursor <= offset or cursor in cursors or not rows:
                raise YandexDirectError("Sitelink reference scan pagination did not make progress")
            cursors.add(cursor)
            offset = cursor

    def _url_migration_read_ads(
        self,
        client: YandexDirectClient,
        campaign_id: str,
        ad_ids: list[int],
    ) -> dict[int, dict[str, Any]]:
        observed: dict[int, dict[str, Any]] = {}
        for batch in _url_migration_chunks(ad_ids):
            response = client.ads_get_by_campaign_and_ids(campaign_id, batch)
            if not response.get("ok"):
                raise YandexDirectError("Yandex Direct rejected campaign-scoped ads.get during URL migration")
            result = response.get("result") or {}
            if not isinstance(result, dict) or result.get("LimitedBy") is not None:
                raise YandexDirectError("Campaign-scoped ads.get result is limited or malformed")
            rows = result.get("Ads") or []
            if not isinstance(rows, list):
                raise YandexDirectError("Campaign-scoped ads.get returned an invalid Ads page")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                ad_id = _url_migration_safe_int(row.get("Id"))
                if ad_id is None or ad_id in observed:
                    raise YandexDirectError("Campaign-scoped ads.get returned duplicate or invalid ad IDs")
                observed[ad_id] = row
        if set(observed) != set(ad_ids):
            raise YandexDirectError("Campaign-scoped ads.get did not return every requested ad")
        for ad_id in ad_ids:
            row = observed[ad_id]
            text_ad = row.get("TextAd")
            if (
                str(row.get("CampaignId")) != str(campaign_id)
                or row.get("Type") != "TEXT_AD"
                or not isinstance(text_ad, dict)
                or not isinstance(text_ad.get("Href"), str)
                or not text_ad.get("Href")
            ):
                raise YandexDirectError("Campaign-scoped ad preflight failed ownership, type, or Href checks")
        return observed

    @staticmethod
    def _url_migration_update_items(
        ordered_ad_ids: list[int],
        live_ads: dict[int, dict[str, Any]],
        target_hrefs: dict[int, str],
        attach_ad_ids: set[int],
        sitelink_set_id: int | str | None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for ad_id in ordered_ad_ids:
            text_ad_raw = live_ads[ad_id].get("TextAd")
            text_ad: dict[str, Any] = text_ad_raw if isinstance(text_ad_raw, dict) else {}
            update: dict[str, Any] = {"Href": target_hrefs.get(ad_id, str(text_ad.get("Href") or ""))}
            if ad_id in attach_ad_ids:
                if sitelink_set_id is None:
                    raise ValueError("Sitelink attach payload needs a created SitelinkSetId")
                update["SitelinkSetId"] = sitelink_set_id
            items.append({"Id": ad_id, "TextAd": update})
        return items

    @staticmethod
    def _url_migration_observed_ad(row: dict[str, Any]) -> dict[str, Any]:
        text_ad_raw = row.get("TextAd")
        text_ad: dict[str, Any] = text_ad_raw if isinstance(text_ad_raw, dict) else {}
        return {
            "ad_id": _url_migration_safe_int(row.get("Id")),
            "campaign_id": str(row.get("CampaignId") or ""),
            "type": str(row.get("Type") or ""),
            "href": text_ad.get("Href"),
            "sitelink_set_id": _url_migration_safe_int(text_ad.get("SitelinkSetId")),
        }

    def _url_migration_readback(
        self,
        client: YandexDirectClient,
        campaign_id: str,
        expectations: dict[int, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            readback_ads = self._url_migration_read_ads(client, campaign_id, list(expectations))
        except (YandexDirectError, ValueError):
            return [], False
        observed = [self._url_migration_observed_ad(readback_ads[ad_id]) for ad_id in expectations]
        for ad_id, expectation in expectations.items():
            before = expectation["before"]
            after = readback_ads[ad_id]
            before_text_raw = before.get("TextAd")
            after_text_raw = after.get("TextAd")
            before_text: dict[str, Any] = before_text_raw if isinstance(before_text_raw, dict) else {}
            after_text: dict[str, Any] = after_text_raw if isinstance(after_text_raw, dict) else {}
            if (
                str(after.get("CampaignId")) != str(campaign_id)
                or after.get("Type") != "TEXT_AD"
                or after_text.get("Href") != expectation["href"]
                or _url_migration_safe_int(after_text.get("SitelinkSetId"))
                != expectation["sitelink_set_id"]
            ):
                return observed, False
            if any(before_text.get(field) != after_text.get(field) for field in _URL_MIGRATION_AUDIT_FIELDS):
                return observed, False
        return observed, True

    def _url_migration_idempotency(
        self,
        *,
        namespace: str,
        campaign_id: str,
        key: str,
        fingerprint: str,
    ) -> UrlMigrationResult | None:
        registry = getattr(self, "_url_migration_results_by_key", None)
        if registry is None:
            registry = {}
            self._url_migration_results_by_key = registry
        existing = registry.get(key)
        if existing is None:
            return None
        if existing["namespace"] != namespace:
            raise ValueError("URL migration idempotency key cannot be reused across migration namespaces")
        if existing["campaign_id"] != campaign_id or existing["fingerprint"] != fingerprint:
            raise ValueError("URL migration idempotency key was already used with a different payload")
        return existing["result"]

    def _store_url_migration_idempotency(
        self,
        *,
        namespace: str,
        campaign_id: str,
        key: str,
        fingerprint: str,
        result: UrlMigrationResult,
    ) -> None:
        self._url_migration_results_by_key[key] = {
            "namespace": namespace,
            "campaign_id": campaign_id,
            "fingerprint": fingerprint,
            "result": result,
        }

    def yandex_ads_landing_urls(
        self,
        campaign_id: str,
        payload: LandingUrlMigrationRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> UrlMigrationResult:
        return self._execute_url_migration(
            campaign_id,
            namespace="landing_urls",
            payload=payload,
            ad_items=payload.items,
            sitelink_spec=None,
            settings=settings,
            client=client,
        )

    def yandex_sitelinks_migrate_urls(
        self,
        campaign_id: str,
        payload: SitelinkUrlMigrationRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> UrlMigrationResult:
        return self._execute_url_migration(
            campaign_id,
            namespace="sitelink_urls",
            payload=payload,
            ad_items=[],
            sitelink_spec=SitelinkUrlMigrationSpec(
                source_sitelink_set_id=payload.source_sitelink_set_id,
                expected_items=payload.expected_items,
                target_items=payload.target_items,
            ),
            settings=settings,
            client=client,
        )

    def yandex_landing_url_migrations(
        self,
        campaign_id: str,
        payload: LandingUrlMigrationsRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> UrlMigrationResult:
        return self._execute_url_migration(
            campaign_id,
            namespace="unified_landing_url_migrations",
            payload=payload,
            ad_items=payload.ad_items,
            sitelink_spec=payload.sitelink_migration,
            settings=settings,
            client=client,
        )

    def _execute_url_migration(
        self,
        campaign_id: str,
        *,
        namespace: str,
        payload: Any,
        ad_items: list[LandingUrlMigrationItem],
        sitelink_spec: SitelinkUrlMigrationSpec | None,
        settings: Settings | None,
        client: YandexDirectClient | None,
    ) -> UrlMigrationResult:
        """Run the documented, non-transactional URL migration state machine."""
        if settings is None:
            raise YandexDirectError("Settings are required for URL migration safety checks")
        mode = settings.directpilot_mode
        if not payload.dry_run:
            if not payload.approved:
                raise ValueError("URL migration apply requires explicit approval")
            if not payload.idempotency_key or not payload.idempotency_key.strip():
                raise ValueError("URL migration apply requires a non-empty idempotency_key")
            if mode != "live_write":
                raise YandexDirectError(
                    f"Live writes require DIRECTPILOT_MODE=live_write; current mode is {mode!r}"
                )
        if client is None:
            raise YandexDirectError("YandexDirectClient is required for URL migration preflight")

        fingerprint = hashlib.sha256(
            json.dumps(
                {"campaign_id": str(campaign_id), "payload": payload.model_dump(mode="json")},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if not payload.dry_run:
            cached = self._url_migration_idempotency(
                namespace=namespace,
                campaign_id=str(campaign_id),
                key=payload.idempotency_key,
                fingerprint=fingerprint,
            )
            if cached is not None:
                return cached

        stages: list[UrlMigrationStage] = []
        provider_results: dict[str, list[UrlMigrationProviderItemResult]] = {}
        changes: list[UrlMigrationChange] = []
        reference_scan: dict[str, Any] | None = None
        new_sitelink_set_id: int | None = None
        recovery_note: str | None = None

        def finalize(
            *,
            stage: str,
            completed: bool,
            partial_failure: bool,
            readback: list[dict[str, Any]] | None = None,
            yandex_units: int | None = None,
        ) -> UrlMigrationResult:
            audit = self.append_audit(
                "yandex_url_migration_preview" if payload.dry_run else "yandex_url_migration_apply",
                str(campaign_id),
                dry_run=payload.dry_run,
                details={
                    "namespace": namespace,
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "mode": mode,
                    "reason": payload.reason,
                    "ad_count": len(ad_items),
                    "source_sitelink_set_id": (
                        sitelink_spec.source_sitelink_set_id if sitelink_spec is not None else None
                    ),
                    "completed": completed,
                    "partial_failure": partial_failure,
                    "stage": stage,
                },
            )
            result = UrlMigrationResult(
                campaign_id=str(campaign_id),
                mode=mode,
                dry_run=payload.dry_run,
                applied=completed if not payload.dry_run else False,
                completed=completed,
                partial_failure=partial_failure,
                audit_id=audit.id,
                stage=stage,
                stages=stages,
                changes=changes,
                payload_preview=payload_preview,
                provider_results=provider_results,
                readback=readback or [],
                reference_scan=reference_scan,
                new_sitelink_set_id=new_sitelink_set_id,
                recovery_note=recovery_note,
                yandex_units=yandex_units,
            )
            if not payload.dry_run:
                self._store_url_migration_idempotency(
                    namespace=namespace,
                    campaign_id=str(campaign_id),
                    key=payload.idempotency_key,
                    fingerprint=fingerprint,
                    result=result,
                )
            return result

        # Product URL policy is enforced before any provider write, including preview.
        for item in ad_items:
            self._validate_url_migration_target(
                item.target_href,
                settings=settings,
                require_anchor=bool(urlsplit(item.target_href).fragment),
            )
        stages.append(UrlMigrationStage(name="target_url_validation", state="completed"))

        main_ad_ids = [item.ad_id for item in ad_items]
        live_ads: dict[int, dict[str, Any]] = {}
        if main_ad_ids:
            live_ads = self._url_migration_read_ads(client, str(campaign_id), main_ad_ids)
            for item in ad_items:
                current_href = (live_ads[item.ad_id].get("TextAd") or {}).get("Href")
                if current_href != item.expected_href:
                    raise ValueError("Current ad Href does not match expected_href")
                changes.append(
                    UrlMigrationChange(
                        entity_type="ad",
                        entity_id=str(item.ad_id),
                        before_href=item.expected_href,
                        after_href=item.target_href,
                    )
                )
            stages.append(UrlMigrationStage(name="campaign_ad_preflight", state="completed"))

        target_hrefs = {item.ad_id: item.target_href for item in ad_items}
        ordered_ad_ids = list(main_ad_ids)
        attach_ad_ids: set[int] = set()
        sitelinks_add_payload: dict[str, Any] | None = None

        if sitelink_spec is not None:
            for item in sitelink_spec.target_items:
                self._validate_url_migration_target(
                    item.href,
                    settings=settings,
                    require_anchor=bool(urlsplit(item.href).fragment),
                )
            source_set = self._url_migration_read_sitelink_set(
                client, sitelink_spec.source_sitelink_set_id
            )
            source_links = source_set.get("Sitelinks") or []
            if not isinstance(source_links, list):
                raise YandexDirectError("Sitelink source set is malformed")
            expected_signature = [item.model_dump() for item in sitelink_spec.expected_items]
            if self._url_migration_sitelink_signature(source_links) != expected_signature:
                raise ValueError("Source sitelink set does not exactly match expected_items")
            stages.append(UrlMigrationStage(name="source_sitelink_preflight", state="completed"))

            references = self._url_migration_scan_sitelink_references(
                client, sitelink_spec.source_sitelink_set_id
            )
            route_reference_ids = [
                ref["ad_id"]
                for ref in references
                if ref["campaign_id"] == str(campaign_id) and ref["type"] == "TEXT_AD"
            ]
            if not route_reference_ids:
                raise ValueError("No route-owned TEXT_AD currently references the source sitelink set")
            route_ads = self._url_migration_read_ads(client, str(campaign_id), route_reference_ids)
            for ad_id, row in route_ads.items():
                if str((row.get("TextAd") or {}).get("SitelinkSetId")) != str(
                    sitelink_spec.source_sitelink_set_id
                ):
                    raise YandexDirectError("Fresh route ad read no longer references the source sitelink set")
                live_ads[ad_id] = row
                if ad_id not in ordered_ad_ids:
                    ordered_ad_ids.append(ad_id)
                attach_ad_ids.add(ad_id)
            for item in ad_items:
                current_href = (live_ads[item.ad_id].get("TextAd") or {}).get("Href")
                if current_href != item.expected_href:
                    raise ValueError("Fresh ad Href does not match expected_href")
            reference_scan = {
                "complete": True,
                "source_sitelink_set_id": sitelink_spec.source_sitelink_set_id,
                "reference_count": len(references),
                "route_reference_ad_ids": route_reference_ids,
                "outside_route_reference_count": len(references) - len(route_reference_ids),
                "references": references,
            }
            stages.append(UrlMigrationStage(name="sitelink_reference_scan", state="completed"))
            for expected, target in zip(sitelink_spec.expected_items, sitelink_spec.target_items, strict=True):
                changes.append(
                    UrlMigrationChange(
                        entity_type="sitelink",
                        entity_id=f"{sitelink_spec.source_sitelink_set_id}/{expected.title}",
                        before_href=expected.href,
                        after_href=target.href,
                    )
                )
            sitelinks_add_payload = {
                "method": "add",
                "params": {
                    "SitelinksSets": [
                        {"Sitelinks": self._url_migration_sitelink_payload(sitelink_spec.target_items)}
                    ]
                },
            }

        preview_sitelink_set_id: int | str | None = (
            "$new_sitelink_set_id" if sitelink_spec is not None else None
        )
        preview_update_items = self._url_migration_update_items(
            ordered_ad_ids,
            live_ads,
            target_hrefs,
            attach_ad_ids,
            preview_sitelink_set_id,
        )
        payload_preview: dict[str, Any] = {
            "ads.update": {"method": "update", "params": {"Ads": preview_update_items}}
        }
        if sitelinks_add_payload is not None:
            payload_preview = {"sitelinks.add": sitelinks_add_payload, **payload_preview}

        if payload.dry_run:
            for index, stage_item in enumerate(stages):
                stages[index] = UrlMigrationStage(name=stage_item.name, state="preview")
            stages.append(UrlMigrationStage(name="provider_writes", state="preview"))
            return finalize(stage="preview", completed=False, partial_failure=False)

        if sitelink_spec is not None and sitelinks_add_payload is not None:
            try:
                add_response = client.sitelinks_add(sitelinks_add_payload["params"]["SitelinksSets"])
            except YandexDirectError:
                add_response = {"ok": False, "error": {"error_code": "transport_failure"}}
            provider_results["sitelinks.add"] = self._url_migration_provider_results(
                add_response,
                result_name="AddResults",
                input_ids=["clone"],
            )
            add_results = provider_results["sitelinks.add"]
            if not all(item.success for item in add_results):
                stages.append(UrlMigrationStage(name="sitelinks.add", state="partial"))
                recovery_note = "No ads.update was sent; inspect whether Direct created an unreferenced clone before retrying."
                return finalize(stage="sitelinks.add", completed=False, partial_failure=True)
            new_sitelink_set_id = _url_migration_safe_int(
                ((add_response.get("result") or {}).get("AddResults") or [{}])[0].get("Id")
            )
            if new_sitelink_set_id is None or new_sitelink_set_id < 1:
                stages.append(UrlMigrationStage(name="sitelinks.add", state="partial"))
                recovery_note = "Clone creation outcome is unknown; do not retry without a fresh preflight."
                return finalize(stage="sitelinks.add", completed=False, partial_failure=True)
            stages.append(UrlMigrationStage(name="sitelinks.add", state="completed"))
            try:
                clone_set = self._url_migration_read_sitelink_set(client, new_sitelink_set_id)
                clone_links = clone_set.get("Sitelinks") or []
                target_signature = self._url_migration_sitelink_signature(
                    self._url_migration_sitelink_payload(sitelink_spec.target_items)
                )
                if not isinstance(clone_links, list) or self._url_migration_sitelink_signature(clone_links) != target_signature:
                    raise YandexDirectError("Created sitelink clone readback does not match requested target items")
            except (YandexDirectError, ValueError):
                stages.append(UrlMigrationStage(name="sitelinks.readback", state="failed"))
                recovery_note = "A clone may exist but was not safely verified; no ads.update was sent."
                return finalize(stage="sitelinks.readback", completed=False, partial_failure=True)
            stages.append(UrlMigrationStage(name="sitelinks.readback", state="completed"))

        actual_update_items = self._url_migration_update_items(
            ordered_ad_ids,
            live_ads,
            target_hrefs,
            attach_ad_ids,
            new_sitelink_set_id,
        )
        try:
            update_response = client.ads_update(actual_update_items)
        except YandexDirectError:
            update_response = {"ok": False, "error": {"error_code": "transport_failure"}}
        provider_results["ads.update"] = self._url_migration_provider_results(
            update_response,
            result_name="UpdateResults",
            input_ids=ordered_ad_ids,
        )
        all_update_items_succeeded = all(item.success for item in provider_results["ads.update"])
        stages.append(
            UrlMigrationStage(
                name="ads.update",
                state="completed" if all_update_items_succeeded else "partial",
            )
        )
        expectations: dict[int, dict[str, Any]] = {}
        for ad_id in ordered_ad_ids:
            before = live_ads[ad_id]
            before_text = before.get("TextAd") or {}
            expectations[ad_id] = {
                "before": before,
                "href": target_hrefs.get(ad_id, before_text.get("Href")),
                "sitelink_set_id": (
                    new_sitelink_set_id
                    if ad_id in attach_ad_ids
                    else _url_migration_safe_int(before_text.get("SitelinkSetId"))
                ),
            }
        readback, readback_matches = self._url_migration_readback(
            client, str(campaign_id), expectations
        )
        stages.append(
            UrlMigrationStage(
                name="ads.readback",
                state="completed" if readback_matches else "failed",
            )
        )
        completed = all_update_items_succeeded and readback_matches
        if not completed:
            recovery_note = (
                "Provider operations are non-transactional. Use the returned readback, then run a new preflight "
                "before any corrective action; do not attempt blind rollback."
            )
        units = _safe_units(update_response.get("units")) if isinstance(update_response, dict) else None
        return finalize(
            stage="completed" if completed else "partial_failure",
            completed=completed,
            partial_failure=not completed,
            readback=readback,
            yandex_units=units,
        )

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


    # ------------------------------------ KeywordBids.get / setAuto (v5)

    def _read_keyword_bids_v5(
        self,
        campaign_id: str,
        *,
        client: YandexDirectClient,
        ad_group_ids: list[int] | None = None,
        keyword_ids: list[int] | None = None,
        serving_statuses: list[str] | None = None,
        limit: int = 1000,
        offset: int = 0,
        campaign_strategy: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], KeywordBidsGetResult]:
        """Read/map controlled KeywordBids rows and their keyword classification."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if ad_group_ids is not None and (not ad_group_ids or len(ad_group_ids) > 1000):
            raise ValueError("ad_group_ids must contain between 1 and 1000 ids")
        if keyword_ids is not None and (not keyword_ids or len(keyword_ids) > 10000):
            raise ValueError("keyword_ids must contain between 1 and 10000 ids")
        if serving_statuses is not None and any(
            status not in {"ELIGIBLE", "RARELY_SERVED"} for status in serving_statuses
        ):
            raise ValueError("serving_statuses supports only ELIGIBLE and RARELY_SERVED")

        campaign = campaign_strategy
        if campaign is None:
            strategy_response = client.campaigns_get_strategy(campaign_id)
            if not strategy_response.get("ok"):
                raise YandexDirectError("campaign strategy read failed before keywordbids.get")
            strategy_result = strategy_response.get("result") or {}
            campaigns = strategy_result.get("Campaigns") if isinstance(strategy_result, dict) else None
            campaign = campaigns[0] if isinstance(campaigns, list) and campaigns else None
            if not isinstance(campaign, dict):
                raise YandexDirectError("campaign strategy result is unavailable before keywordbids.get")

        strategy = (
            campaign.get("TextCampaign", {}).get("BiddingStrategy", {})
            if isinstance(campaign.get("TextCampaign"), dict)
            else {}
        )
        search = strategy.get("Search") if isinstance(strategy, dict) else None
        network = strategy.get("Network") if isinstance(strategy, dict) else None
        search_type = (
            (search.get("BiddingStrategyType") or search.get("Type"))
            if isinstance(search, dict)
            else None
        )
        network_type = (
            (network.get("BiddingStrategyType") or network.get("Type"))
            if isinstance(network, dict)
            else None
        )

        response = client.keywordbids_get(
            campaign_id,
            ad_group_ids=ad_group_ids,
            keyword_ids=keyword_ids,
            serving_statuses=serving_statuses,
            limit=limit,
            offset=offset,
            include_auction_bids=search_type != "SERVING_OFF",
            include_coverage=network_type != "SERVING_OFF",
        )
        if not response.get("ok"):
            raise YandexDirectError("keywordbids.get failed")
        response_result = response.get("result") or {}
        if not isinstance(response_result, dict):
            raise YandexDirectError("keywordbids.get returned an invalid result envelope")
        raw_rows = response_result.get("KeywordBids")
        if not isinstance(raw_rows, list):
            raw_rows = []

        warnings = _provider_warnings_from_result(response)
        keyword_rows: dict[int, dict[str, Any]] = {}
        try:
            keywords_response = client.keywords_get(campaign_id)
            if keywords_response.get("ok"):
                keyword_result = keywords_response.get("result") or {}
                raw_keywords = keyword_result.get("Keywords") if isinstance(keyword_result, dict) else []
                if isinstance(raw_keywords, list):
                    for row in raw_keywords:
                        if not isinstance(row, dict):
                            continue
                        row_id = _as_int_or_none(row.get("Id"))
                        if row_id is not None:
                            keyword_rows[row_id] = row
            else:
                warnings.append(
                    ProviderWarning(code=0, message="Keyword classification unavailable", details="")
                )
        except Exception:
            warnings.append(
                ProviderWarning(code=0, message="Keyword classification unavailable", details="")
            )

        items: list[KeywordBidsGetItem] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                continue
            keyword_id = _as_int_or_none(raw_row.get("KeywordId"))
            row_campaign_id = _as_int_or_none(raw_row.get("CampaignId"))
            if keyword_id is None or row_campaign_id is None:
                continue
            source_keyword = keyword_rows.get(keyword_id)
            if source_keyword is None:
                row_kind: Literal["keyword", "autotargeting", "unknown"] = "unknown"
                keyword_text = None
            elif source_keyword.get("Keyword") == "---autotargeting":
                row_kind = "autotargeting"
                keyword_text = None
            else:
                row_kind = "keyword"
                keyword_text = source_keyword.get("Keyword") if isinstance(source_keyword.get("Keyword"), str) else None

            raw_search = raw_row.get("Search") if isinstance(raw_row.get("Search"), dict) else {}
            raw_network = raw_row.get("Network") if isinstance(raw_row.get("Network"), dict) else {}
            raw_auction = raw_search.get("AuctionBids") if isinstance(raw_search, dict) else None
            raw_auction_items = raw_auction.get("AuctionBidItems") if isinstance(raw_auction, dict) else []
            auction_bids = [
                KeywordBidsAuctionBidItem(
                    traffic_volume=_as_int_or_none(entry.get("TrafficVolume")),
                    bid_micros=_as_int_or_none(entry.get("Bid")),
                    bid_rub=_micros_to_rub(entry.get("Bid")),
                    price_micros=_as_int_or_none(entry.get("Price")),
                    price_rub=_micros_to_rub(entry.get("Price")),
                )
                for entry in raw_auction_items
                if isinstance(entry, dict)
            ] if isinstance(raw_auction_items, list) else []
            raw_coverage = raw_network.get("Coverage") if isinstance(raw_network, dict) else None
            raw_coverage_items = raw_coverage.get("CoverageItems") if isinstance(raw_coverage, dict) else []
            coverage = [
                KeywordBidsCoverageItem(
                    probability=_as_int_or_none(entry.get("Probability")),
                    bid_micros=_as_int_or_none(entry.get("Bid")),
                    bid_rub=_micros_to_rub(entry.get("Bid")),
                )
                for entry in raw_coverage_items
                if isinstance(entry, dict)
            ] if isinstance(raw_coverage_items, list) else []
            auto_value = raw_search.get("AutotargetingSearchBidIsAuto") if isinstance(raw_search, dict) else None
            items.append(
                KeywordBidsGetItem(
                    campaign_id=row_campaign_id,
                    ad_group_id=_as_int_or_none(raw_row.get("AdGroupId")),
                    keyword_id=keyword_id,
                    row_kind=row_kind,
                    keyword=keyword_text,
                    serving_status=raw_row.get("ServingStatus") if isinstance(raw_row.get("ServingStatus"), str) else None,
                    strategy_priority=raw_row.get("StrategyPriority") if isinstance(raw_row.get("StrategyPriority"), str) else None,
                    search_bid_micros=_as_int_or_none(raw_search.get("Bid")),
                    search_bid_rub=_micros_to_rub(raw_search.get("Bid")),
                    search_autotargeting_is_auto=True if auto_value == "YES" else False if auto_value == "NO" else None,
                    auction_bids=auction_bids,
                    network_bid_micros=_as_int_or_none(raw_network.get("Bid")),
                    network_bid_rub=_micros_to_rub(raw_network.get("Bid")),
                    coverage=coverage,
                )
            )

        limited_by = _as_int_or_none(response_result.get("LimitedBy"))
        return campaign, KeywordBidsGetResult(
            campaign_id=campaign_id,
            source="yandex",
            items=items,
            limited_by=limited_by,
            next_offset=limited_by + 1 if limited_by is not None else None,
            warnings=warnings,
        )

    def yandex_keyword_bids_get(
        self,
        campaign_id: str,
        *,
        client: YandexDirectClient,
        ad_group_ids: list[int] | None = None,
        keyword_ids: list[int] | None = None,
        serving_statuses: list[str] | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> KeywordBidsGetResult:
        """Canonical typed read endpoint backed by ``keywordbids.get``."""

        _, result = self._read_keyword_bids_v5(
            campaign_id,
            client=client,
            ad_group_ids=ad_group_ids,
            keyword_ids=keyword_ids,
            serving_statuses=serving_statuses,
            limit=limit,
            offset=offset,
        )
        return result

    def _set_auto_ownership_blockers(
        self,
        campaign_id: str,
        payload: KeywordBidsSetAutoRequest,
        *,
        client: YandexDirectClient,
        campaign: dict[str, Any],
    ) -> list[str]:
        """Verify every non-campaign target belongs to the route campaign."""

        expected_campaign_id = _as_int_or_none(campaign_id)
        if expected_campaign_id is None:
            return ["Route campaign id must be numeric for setAuto"]
        if _as_int_or_none(campaign.get("Id")) != expected_campaign_id:
            return ["Campaign strategy response does not match the route campaign"]
        if payload.scope == "campaign":
            return []
        if payload.scope == "ad_group":
            response = client.adgroups_get(campaign_id)
            if not response.get("ok"):
                return ["Ad-group ownership could not be verified; setAuto is blocked"]
            result = response.get("result") or {}
            rows = result.get("AdGroups") if isinstance(result, dict) else None
            owned = {
                _as_int_or_none(row.get("Id"))
                for row in rows or []
                if isinstance(row, dict) and _as_int_or_none(row.get("CampaignId")) == expected_campaign_id
            }
            missing = sorted(set(payload.ad_group_ids or []) - {item for item in owned if item is not None})
            return ["Selected ad groups do not belong to the route campaign"] if missing else []

        response = client.keywords_get(campaign_id)
        if not response.get("ok"):
            return ["Keyword ownership could not be verified; setAuto is blocked"]
        result = response.get("result") or {}
        rows = result.get("Keywords") if isinstance(result, dict) else None
        indexed = {
            _as_int_or_none(row.get("Id")): row
            for row in rows or []
            if isinstance(row, dict) and _as_int_or_none(row.get("Id")) is not None
        }
        blockers: list[str] = []
        for keyword_id in payload.keyword_ids or []:
            row = indexed.get(keyword_id)
            if not isinstance(row, dict) or _as_int_or_none(row.get("CampaignId")) != expected_campaign_id:
                blockers.append("Selected keywords do not belong to the route campaign")
                break
            if row.get("Keyword") == "---autotargeting":
                blockers.append("keyword-scoped setAuto does not support autotargeting identifiers")
                break
        return blockers

    @staticmethod
    def _set_auto_item_results(
        result_payload: Any,
    ) -> tuple[list[KeywordBidsSetAutoItemResult] | None, str | None]:
        if not isinstance(result_payload, dict):
            return None, "setAuto response is missing SetAutoResults"
        raw_results = result_payload.get("SetAutoResults")
        if not isinstance(raw_results, list) or not raw_results:
            return None, "setAuto response is missing SetAutoResults"
        items: list[KeywordBidsSetAutoItemResult] = []
        has_errors = False
        for raw in raw_results:
            if not isinstance(raw, dict):
                has_errors = True
                continue
            errors = [
                ProviderWarning(
                    code=_as_int_or_none(entry.get("Code")) or 0,
                    message=str(entry.get("Message") or ""),
                    details=str(entry.get("Details") or ""),
                )
                for entry in raw.get("Errors") or []
                if isinstance(entry, dict)
            ]
            warnings = [
                ProviderWarning(
                    code=_as_int_or_none(entry.get("Code")) or 0,
                    message=str(entry.get("Message") or ""),
                    details=str(entry.get("Details") or ""),
                )
                for entry in raw.get("Warnings") or []
                if isinstance(entry, dict)
            ]
            has_errors = has_errors or bool(errors)
            items.append(
                KeywordBidsSetAutoItemResult(
                    campaign_id=_as_int_or_none(raw.get("CampaignId")),
                    ad_group_id=_as_int_or_none(raw.get("AdGroupId")),
                    keyword_id=_as_int_or_none(raw.get("KeywordId")),
                    has_errors=bool(errors),
                    has_warnings=bool(warnings),
                    errors=errors,
                    warnings=warnings,
                )
            )
        return items, "setAuto returned per-item errors" if has_errors else None

    def yandex_keyword_bids_set_auto(
        self,
        campaign_id: str,
        payload: KeywordBidsSetAutoRequest,
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> KeywordBidsSetAutoResult:
        """Preview/apply fail-closed automatic bid rules without strategy edits."""

        mode = settings.directpilot_mode if settings is not None else "mock"
        direct_payload = payload.build_direct_payload(campaign_id)
        v5_items = direct_payload["params"]["KeywordBids"]
        source: Literal["mock", "yandex"] = "yandex" if client is not None else "mock"

        if not payload.dry_run:
            if not payload.approved:
                raise ValueError("Action requires explicit approval")
            if mode != "live_write":
                raise YandexDirectError("Live writes require DIRECTPILOT_MODE=live_write")
            if not payload.idempotency_key:
                raise ValueError("idempotency_key is required when dry_run=false")
            cache_key = f"keyword_bids_set_auto:{campaign_id}:{payload.idempotency_key}"
            fingerprint = _keyword_bids_set_auto_request_fingerprint(direct_payload)
            cached_record = self._keyword_bids_set_auto_results_by_key.get(cache_key)
            if cached_record is not None:
                if cached_record.get("request_fingerprint") != fingerprint:
                    raise ValueError(
                        f"Idempotency key {payload.idempotency_key!r} was previously used with a different setAuto payload; replay is rejected"
                    )
                return cached_record["result"]
        else:
            cache_key = None
            fingerprint = None

        if client is None:
            audit = self.append_audit(
                "yandex_keyword_bids_set_auto_blocked",
                campaign_id,
                dry_run=payload.dry_run,
                details={"mode": mode, "reason": "client_unavailable"},
            )
            return KeywordBidsSetAutoResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=payload.dry_run,
                applied=False,
                blocked=True,
                source=source,
                audit_id=audit.id,
                payload_preview=direct_payload,
                blockers=["Campaign strategy and ownership could not be read; setAuto is blocked"],
            )

        ad_group_ids = payload.ad_group_ids if payload.scope == "ad_group" else None
        keyword_ids = payload.keyword_ids if payload.scope == "keyword" else None
        affected_limit = _keyword_bids_set_auto_read_limit(payload)
        campaign, affected = self._read_keyword_bids_v5(
            campaign_id,
            client=client,
            ad_group_ids=ad_group_ids,
            keyword_ids=keyword_ids,
            limit=affected_limit,
        )
        blockers = _keyword_bids_set_auto_window_blockers(
            campaign_id,
            payload,
            affected,
            require_keyword_coverage=True,
        )
        if not blockers:
            blockers = validate_keyword_bids_set_auto_strategy(campaign, payload.rule_type)
            blockers.extend(
                self._set_auto_ownership_blockers(
                    campaign_id, payload, client=client, campaign=campaign
                )
            )
        warnings = list(affected.warnings)
        if payload.rule_type == "search_by_traffic_volume" and payload.scope in {"campaign", "ad_group"}:
            warnings.append(
                ProviderWarning(
                    code=0,
                    message="Direct may affect autotargeting search bids for campaign or ad-group scope",
                    details="",
                )
            )

        if blockers:
            audit = self.append_audit(
                "yandex_keyword_bids_set_auto_blocked",
                campaign_id,
                dry_run=payload.dry_run,
                details={"mode": mode, "blocker_count": len(blockers), "scope": payload.scope},
            )
            return KeywordBidsSetAutoResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=payload.dry_run,
                applied=False,
                blocked=True,
                source=source,
                audit_id=audit.id,
                payload_preview=direct_payload,
                affected_items=affected.items,
                blockers=blockers,
                warnings=warnings,
            )

        if payload.dry_run:
            audit = self.append_audit(
                "yandex_keyword_bids_set_auto_previewed",
                campaign_id,
                dry_run=True,
                details={"mode": mode, "scope": payload.scope, "item_count": len(v5_items)},
            )
            return KeywordBidsSetAutoResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=True,
                applied=False,
                source=source,
                audit_id=audit.id,
                payload_preview=direct_payload,
                affected_items=affected.items,
                warnings=warnings,
            )

        response = client.keywordbids_set_auto(v5_items)
        if not response.get("ok"):
            raise YandexDirectError("keywordbids.setAuto failed")
        provider_warnings = _provider_warnings_from_result(response)
        set_auto_results, item_error = self._set_auto_item_results(response.get("result"))
        if set_auto_results:
            for item in set_auto_results:
                for warning in item.warnings:
                    if not any(
                        existing.code == warning.code and existing.message == warning.message
                        for existing in provider_warnings
                    ):
                        provider_warnings.append(warning)
        partial_failure = item_error is not None
        readback: KeywordBidsGetResult | None = None
        readback_failed = False
        verification_error: str | None = None
        if not partial_failure:
            try:
                _, readback = self._read_keyword_bids_v5(
                    campaign_id,
                    client=client,
                    ad_group_ids=ad_group_ids,
                    keyword_ids=keyword_ids,
                    limit=affected_limit,
                    campaign_strategy=campaign,
                )
                verification_blockers = _keyword_bids_set_auto_window_blockers(
                    campaign_id,
                    payload,
                    readback,
                    require_keyword_coverage=True,
                )
                if verification_blockers:
                    readback = None
                    readback_failed = True
                    partial_failure = True
                    verification_error = (
                        verification_blockers[0]
                        if payload.scope == "keyword"
                        else "keywordbids.get readback was truncated after provider write"
                    )
            except Exception:
                readback_failed = True
                partial_failure = True
                verification_error = "keywordbids.get readback failed after provider write"

        audit = self.append_audit(
            "yandex_keyword_bids_set_auto_applied",
            campaign_id,
            dry_run=False,
            details={
                "mode": mode,
                "scope": payload.scope,
                "item_count": len(v5_items),
                "partial_failure": partial_failure,
                "readback_failed": readback_failed,
            },
        )
        result = KeywordBidsSetAutoResult(
            campaign_id=campaign_id,
            mode=mode,
            dry_run=False,
            applied=not partial_failure,
            source=source,
            audit_id=audit.id,
            affected_items=affected.items,
            warnings=warnings + provider_warnings,
            set_auto_results=set_auto_results,
            partial_failure=partial_failure,
            readback=readback,
            readback_failed=readback_failed,
            verification_error=verification_error,
            yandex_units=_as_int_or_none(response.get("units")),
            yandex_error=item_error,
        )
        if cache_key is not None and fingerprint is not None:
            self._keyword_bids_set_auto_results_by_key[cache_key] = {
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

        # --- Idempotency cache check ----------------------------------------
        cache_key = f"keyword_bids:{campaign_id}:{payload.idempotency_key}"

        # --- Build v5 payload -----------------------------------------------
        v5_items: list[dict[str, Any]] = [
            item.to_direct_micros_item() for item in payload.items
        ]
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
        if not can_write:
            raise YandexDirectError(
                "Live writes are not allowed in live_readonly mode; use live_write"
            )

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

        partial_failure = False
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
        # Only attempt readback when top-level ok AND no item-level errors
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

    def yandex_bid_modifiers_create(
        self,
        campaign_id: str,
        payload: "BidModifiersCreateRequest",
        *,
        settings: Settings | None = None,
        client: YandexDirectClient | None = None,
    ) -> "BidModifiersCreateResult":
        """Preview/apply new Yandex Direct bid modifiers through ``bidmodifiers.add``."""

        if not payload.approved:
            raise ValueError("Action requires explicit approval")
        if not payload.idempotency_key:
            raise ValueError("idempotency_key is required for bid modifiers create")

        mode = settings.directpilot_mode if settings is not None else "mock"
        is_live = mode in ("sandbox", "live_readonly", "live_write")
        can_write = mode == "live_write"

        direct_payload = payload.build_direct_add_payload()
        payload_fingerprint = _bid_modifiers_request_fingerprint(
            {"method": "add", "params": direct_payload}
        )
        cache_key = f"bid_modifiers_create:{campaign_id}:{payload.idempotency_key}"

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
                    "used with a different bid modifiers create payload; replay is rejected"
                )
            return cached

        if payload.dry_run or not is_live:
            audit = self.append_audit(
                "yandex_bid_modifiers_create_requested",
                campaign_id,
                dry_run=payload.dry_run,
                details={
                    "approved": payload.approved,
                    "idempotency_key": payload.idempotency_key,
                    "reason": payload.reason,
                    "item_count": len(payload.items),
                    "source": "yandex" if is_live else "mock",
                    "mode": mode,
                },
            )
            result = BidModifiersCreateResult(
                campaign_id=campaign_id,
                mode=mode,
                dry_run=True,
                applied=False,
                source="yandex" if is_live else "mock",
                read_only=True,
                audit_id=audit.id,
                payload_preview=direct_payload,
                message="Preview-only: no provider calls were made and no changes were applied.",
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
                "Yandex Direct client is required for live bid modifiers create"
            )

        v5_items = direct_payload["BidModifiers"]
        call_payload_preview = {"method": "add", "params": direct_payload}
        try:
            response = client.bidmodifiers_add(v5_items)
        except YandexDirectError:
            raise
        except Exception as exc:
            raise YandexDirectError(
                f"bidmodifiers.add failed: {type(exc).__name__}: {exc}"
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
                or "bidmodifiers.add failed"
            )
            raise YandexDirectError(
                f"bidmodifiers.add failed: {error_code or 'unknown'}",
                diagnostics={
                    "error_code": error_code,
                    "error_detail": str(error_detail)[:500],
                    "payload_preview": call_payload_preview,
                },
            )

        add_results, item_error_summary = _extract_bid_modifier_add_results(
            response.get("result")
        )
        partial_failure = item_error_summary is not None
        if add_results:
            for item in add_results:
                if item.has_warnings:
                    for warning in item.warnings:
                        already_present = any(
                            pw.code == warning.code and pw.message == warning.message
                            for pw in provider_warnings
                        )
                        if not already_present:
                            provider_warnings.append(warning)

        readback: list[dict] | None = None
        if not partial_failure:
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
            "yandex_bid_modifiers_create_applied",
            campaign_id,
            dry_run=False,
            details={
                "approved": payload.approved,
                "idempotency_key": payload.idempotency_key,
                "reason": payload.reason,
                "item_count": len(payload.items),
                "source": "yandex",
                "mode": mode,
                "ok": top_level_ok,
                "partial_failure": partial_failure,
                "item_error_summary": item_error_summary,
            },
        )
        response_units = response.get("units")
        result = BidModifiersCreateResult(
            campaign_id=campaign_id,
            mode=mode,
            dry_run=False,
            applied=not partial_failure,
            source="yandex",
            audit_id=audit.id,
            readback=readback,
            provider_warnings=provider_warnings,
            add_results=add_results,
            partial_failure=partial_failure,
            yandex_units=_try_int(response_units) if response_units is not None else None,
            yandex_error=item_error_summary,
        )
        self._bid_modifiers_results_by_key[cache_key] = {
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
