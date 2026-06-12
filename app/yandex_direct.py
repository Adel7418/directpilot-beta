from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from app.config import Settings

SANDBOX_BASE_URL = "https://api-sandbox.direct.yandex.com/json/v5"
LIVE_BASE_URL = "https://api.direct.yandex.com/json/v5"


class YandexDirectError(RuntimeError):
    pass


class YandexDirectClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.Client(timeout=20, transport=transport)

    @property
    def base_url(self) -> str:
        if self.settings.directpilot_mode == "sandbox":
            return SANDBOX_BASE_URL
        return LIVE_BASE_URL

    def clients_get(self) -> dict[str, Any]:
        payload = {
            "method": "get",
            "params": {
                "FieldNames": ["Login", "ClientId"],
            },
        }
        return self._call("clients", payload)

    def campaigns_get(self) -> dict[str, Any]:
        payload = {
            "method": "get",
            "params": {
                "SelectionCriteria": {},
                "FieldNames": ["Id", "Name", "Status", "State", "Type", "DailyBudget"],
            },
        }
        return self._call("campaigns", payload)

    @staticmethod
    def _direct_id(value: int | str) -> int | str:
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value

    # ------------------------------------------------------------------
    # Live-create (campaigns.add)
    #
    # Direct API v5 `campaigns.add` (see
    # https://yandex.com/dev/direct/doc/ref-v5/campaigns/add.html)
    # creates a real Yandex Direct campaign. The endpoint is the
    # single most safety-sensitive write in the product: a single
    # mis-call here can create a billable campaign on the user's
    # production account. The `campaigns.add` helper is therefore
    # kept deliberately small — it accepts a fully-shaped v5
    # ``Campaigns`` list and forwards it to the v5 service with
    # ``method=add``. The store / endpoint layer is responsible for
    # building the input from a campaign-draft preview; the client
    # never invents field names that the v5 contract does not
    # document.
    # ------------------------------------------------------------------

    def campaigns_add(self, campaigns: list[dict[str, Any]]) -> dict[str, Any]:
        """Create one or more Yandex Direct campaigns via v5 ``campaigns.add``.

        ``campaigns`` must be a list of fully-shaped v5 ``Campaigns``
        dictionaries (typically built by the store / endpoint from a
        :class:`CampaignDraft` preview). The helper forwards the
        payload to the v5 ``campaigns`` service with ``method=add``
        and returns the same ``{ok, result, error, units}`` envelope
        used by the rest of the client.

        The helper does NOT call ``adgroups.add`` / ``ads.add`` /
        ``keywords.add`` — those are separate v5 services and a
        follow-up campaign-create flow will chain them. Splitting
        the stages keeps a single failure from leaving a partial
        campaign on the user's account: each stage can be
        individually gated and audited.
        """
        return self._call(
            "campaigns",
            {"method": "add", "params": {"Campaigns": list(campaigns)}},
        )

    # ------------------------------------------------------------------
    # Read-only child entities (adgroups / ads / keywords)
    #
    # All three are GET methods on Direct API v5. They accept
    # SelectionCriteria.CampaignIds and return their result objects wrapped
    # in the same {ok, result, units, error} envelope as campaigns_get.
    #

    def adgroups_get(self, campaign_id: int | str) -> dict[str, Any]:
        campaign_id = self._direct_id(campaign_id)
        # NegativeKeywords is requested here so the semantic-change apply
        # path can merge the user's requested phrases with the live
        # group-level negative set on Direct (adgroups.update with
        # NegativeKeywords.Items is REPLACE, not APPEND). Without this
        # field the merge would be a blind overwrite of any pre-existing
        # negative keywords.
        payload = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"CampaignIds": [campaign_id]},
                "FieldNames": [
                    "Id",
                    "Name",
                    "CampaignId",
                    "Status",
                    "ServingStatus",
                    "Type",
                    "RegionIds",
                    "NegativeKeywords",
                ],
            },
        }
        return self._call("adgroups", payload)

    def ads_get(self, campaign_id: int | str) -> dict[str, Any]:
        campaign_id = self._direct_id(campaign_id)
        payload = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"CampaignIds": [campaign_id]},
                "FieldNames": [
                    "Id",
                    "AdGroupId",
                    "CampaignId",
                    "Status",
                    "State",
                    "Type",
                ],
                "TextAdFieldNames": ["Title", "Text", "Href"],
            },
        }
        return self._call("ads", payload)

    def keywords_get(self, campaign_id: int | str) -> dict[str, Any]:
        campaign_id = self._direct_id(campaign_id)
        payload = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"CampaignIds": [campaign_id]},
                "FieldNames": [
                    "Id",
                    "AdGroupId",
                    "CampaignId",
                    "Keyword",
                    "Bid",
                    "ContextBid",
                    "StrategyPriority",
                    "State",
                    "Status",
                    "ServingStatus",
                ],
            },
        }
        return self._call("keywords", payload)

    # ------------------------------------------------------------------
    # Extended read-only coverage for DirectPilot's "everything" layer.
    # These wrappers intentionally expose raw Direct API results. The app
    # keeps them read-only and redacts transport details on errors.
    # ------------------------------------------------------------------

    def _selection_by_campaign(self, campaign_id: int | str) -> dict[str, Any]:
        return {"CampaignIds": [self._direct_id(campaign_id)]}

    def bids_get(self, campaign_id: int | str) -> dict[str, Any]:
        return self._call(
            "bids",
            {
                "method": "get",
                "params": {
                    "SelectionCriteria": self._selection_by_campaign(campaign_id),
                    "FieldNames": ["KeywordId", "AdGroupId", "CampaignId", "Bid", "ContextBid"],
                },
            },
        )

    def changes_check(self) -> dict[str, Any]:
        return self._call("changes", {"method": "check", "params": {}})

    def changes_get(self) -> dict[str, Any]:
        return self._call("changes", {"method": "get", "params": {}})

    def dictionaries_get(self) -> dict[str, Any]:
        return self._call(
            "dictionaries",
            {
                "method": "get",
                "params": {
                    "DictionaryNames": [
                        "Currencies",
                        "GeoRegions",
                        "TimeZones",
                        "Constants",
                        "AdCategories",
                    ]
                },
            },
        )

    def bidmodifiers_get(self, campaign_id: int | str) -> dict[str, Any]:
        return self._call(
            "bidmodifiers",
            {"method": "get", "params": {"SelectionCriteria": self._selection_by_campaign(campaign_id)}},
        )

    def negativekeywords_get(self, campaign_id: int | str, ids: list[int]) -> dict[str, Any]:
        # Direct API v5 exposes account-level negative keyword shared sets as
        # `negativekeywordsharedsets`. Campaign/ad-group minus phrases are
        # represented as fields on their parent entities, so this wrapper gives
        # DirectPilot the available read-only negative-keyword object surface.
        _ = campaign_id
        return self._call(
            "negativekeywordsharedsets",
            {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": ids},
                    "FieldNames": ["Id", "Name", "NegativeKeywords"],
                },
            },
        )

    def retargetinglists_get(self) -> dict[str, Any]:
        return self._call("retargetinglists", {"method": "get", "params": {"SelectionCriteria": {}}})

    def audiencetargets_get(self, campaign_id: int | str) -> dict[str, Any]:
        return self._call(
            "audiencetargets",
            {"method": "get", "params": {"SelectionCriteria": self._selection_by_campaign(campaign_id)}},
        )

    def sitelinks_get(self) -> dict[str, Any]:
        return self._call("sitelinks", {"method": "get", "params": {"SelectionCriteria": {}}})

    def vcards_get(self) -> dict[str, Any]:
        return self._call(
            "vcards",
            {
                "method": "get",
                "params": {
                    "SelectionCriteria": {},
                    "FieldNames": [
                        "Id",
                        "Country",
                        "City",
                        "CompanyName",
                        "WorkTime",
                        "Phone",
                        "ContactPerson",
                        "Street",
                        "House",
                    ],
                },
            },
        )

    def vcards_add(self, vcard: dict[str, Any]) -> dict[str, Any]:
        return self._call("vcards", {"method": "add", "params": {"VCards": [vcard]}})

    # ------------------------------------------------------------------
    # Semantic-change write helpers
    #
    # Direct API v5 confirmed contract (see
    # https://yandex.ru/dev/direct/doc/en/keywords/add.html and
    # .../adgroups/update.html):
    #
    # * ``keywords.add`` — service ``keywords``, method ``add``,
    #   params ``{"Keywords": [{"Keyword": str, "AdGroupId": long,
    #   optional Bid/ContextBid/...}]}``. Max 1000 keywords per call,
    #   duplicates are silently dropped, and the keyword string may
    #   include negative words prefixed with ``-`` (the API itself
    #   normalises that).
    # * ``adgroups.update`` — service ``adgroups``, method ``update``,
    #   params ``{"AdGroups": [{"Id": long, "NegativeKeywords":
    #   {"Items": [str, ...]}}]}``. The ``NegativeKeywords.Items`` list
    #   is the group-level shared negative-keyword set; phrases must
    #   be supplied WITHOUT a leading ``-`` (Direct treats them as
    #   negative by position, not by prefix) and the combined length
    #   must not exceed 4096 chars.
    #
    # These helpers are the ONLY way the store is allowed to call
    # those v5 services. The store never touches the private ``_call``
    # for semantic changes — keeping the call surface explicit and
    # auditable.
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_negative_phrase(phrase: str) -> str:
        """Strip a leading ``-`` and any surrounding whitespace.

        Direct API v5 expects ``NegativeKeywords.Items`` to be plain
        positive-form phrases (``бесплатно``, not ``-бесплатно``). The
        original store code accepted user input with a leading ``-``
        for symmetry with positive keywords; we normalise here so the
        upstream payload is exactly what Direct expects.
        """
        return phrase.strip().lstrip("-").strip()

    def keywords_add(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Add positive keywords to existing ad groups.

        ``items`` must be a list of ``{"Keyword": str, "AdGroupId": long}``
        dictionaries (optional ``Bid`` / ``ContextBid`` etc. are passed
        through unchanged). The payload is sent to the v5 ``keywords``
        service with ``method=add``.
        """
        return self._call("keywords", {"method": "add", "params": {"Keywords": list(items)}})

    # ------------------------------------------------------------------
    # Live-create chain (stage 2 / stage 3)
    #
    # The live-create flow chains four v5 services behind one
    # endpoint: ``campaigns.add`` → ``adgroups.add`` → ``ads.add`` →
    # ``keywords.add``. Stage 5 (``negativekeywordsharedsets.add``) is
    # intentionally NOT implemented here — the v5 shape is not
    # documented in the project sources and the read-only path for
    # shared sets is read-only by design. Each new helper is
    # deliberately small: it accepts a fully-shaped v5 list and
    # forwards it to the v5 service with ``method=add``. The store /
    # endpoint layer is responsible for building the input from a
    # :class:`CampaignDraft` preview; the client never invents
    # field names that the v5 contract does not document.
    # ------------------------------------------------------------------

    def adgroups_add(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Create one or more Yandex Direct ad groups via v5 ``adgroups.add``.

        ``items`` must be a list of fully-shaped v5 ``AdGroups``
        dictionaries (typically built by the store / endpoint from a
        :class:`CampaignDraft` preview). The helper forwards the
        payload to the v5 ``adgroups`` service with ``method=add`` and
        returns the same ``{ok, result, error, units}`` envelope used
        by the rest of the client.

        The Direct API v5 contract:

        .. code-block:: json

            {"method": "add", "params": {"AdGroups": [
                {"Name": "...", "CampaignId": 123, "RegionIds": [...],
                 "NegativeKeywords": {"Items": [...]}},
                ...
            ]}}

        Each item is added with the v5 service's defaults for any
        field the caller does not supply (e.g. ``Status`` defaults to
        ``DRAFT`` for new ad groups).
        """
        return self._call(
            "adgroups", {"method": "add", "params": {"AdGroups": list(items)}}
        )

    def ads_add(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Create one or more Yandex Direct text ads via v5 ``ads.add``.

        ``items`` must be a list of fully-shaped v5 ``Ads`` entries,
        each carrying the target ``AdGroupId`` and a ``TextAd`` block
        (``Title`` / ``Text`` / ``Href`` / optional ``DisplayLinkPath``).
        The helper forwards the payload to the v5 ``ads`` service with
        ``method=add`` and returns the standard envelope.

        The Direct API v5 contract for a text ad:

        .. code-block:: json

            {"method": "add", "params": {"Ads": [
                {"AdGroupId": 123,
                 "TextAd": {"Title": "...", "Text": "...", "Href": "...",
                            "DisplayLinkPath": "..." /* optional */}},
                ...
            ]}}
        """
        return self._call("ads", {"method": "add", "params": {"Ads": list(items)}})

    # ------------------------------------------------------------------
    # ads.update — safe-by-default write helper for attaching an
    # existing Yandex Business organization to a TextAd.
    #
    # Direct API v5 ``ads.update`` (see
    # https://yandex.com/dev/direct/doc/ref-v5/ads/update.html) is a
    # REPLACE-shaped call: every field the operator wants to keep
    # on the ad MUST be re-sent in the same request. The Direct
    # docs document the TextAd body as requiring ``Title``,
    # ``Text``, and ``Href``; optional fields include
    # ``DisplayLinkPath``, ``AdImageHash``, ``VideoExtension``,
    # ``BusinessId`` (long), and ``PreferVCardOverBusiness``
    # (``YES`` / ``NO``).
    #
    # This helper is intentionally narrow: the caller (the store /
    # endpoint layer) builds a fully-shaped v5 ``Ads`` list and
    # forwards it. We never invent field names that the v5 contract
    # does not document. The contract for the BusinessId-attach
    # use case is:
    #
    # .. code-block:: json
    #
    #     {"method": "update", "params": {"Ads": [
    #         {"Id": 99001,
    #          "TextAd": {"Title": "...", "Text": "...", "Href": "...",
    #                     "BusinessId": 11588384335,
    #                     "PreferVCardOverBusiness": "NO"}},
    #         ...
    #     ]}}
    #
    # The BusinessId attach path is preferred over ``vcards.add`` for
    # organization-level contact information because
    # ``vcards.add`` can fail with ``error_code=3500`` for several
    # account types — see the note in ``docs/API_SIMPLE.md`` for the
    # rationale.
    # ------------------------------------------------------------------

    def ads_update(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Update one or more Yandex Direct ads via v5 ``ads.update``.

        ``items`` must be a list of fully-shaped v5 ``Ads``
        dictionaries. Each entry carries the target ``Id`` and the
        v5 sub-block(s) to update (``TextAd`` / ``MobileAppAd`` /
        ``DynamicTextAd`` / etc.). The helper forwards the payload
        to the v5 ``ads`` service with ``method=update`` and
        returns the same ``{ok, result, error, units}`` envelope
        used by the rest of the client.

        The BusinessId-attach use case is the canonical caller. The
        store layer reads the live ad via ``ads.get`` (so the
        required ``Title`` / ``Text`` / ``Href`` fields can be
        re-sent in the same REPLACE-shaped call), then forwards
        the items here. ``DisplayLinkPath`` is OPTIONAL; we never
        invent it.
        """
        return self._call("ads", {"method": "update", "params": {"Ads": list(items)}})

    def adgroups_update(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Update ad groups, currently used for shared negative keywords.

        ``items`` is a list of ``{"Id": long, ...}`` dictionaries; the
        caller is expected to supply ``NegativeKeywords.Items`` (already
        normalised — no leading ``-``) when used from the semantic-change
        pipeline. The payload is sent to the v5 ``adgroups`` service
        with ``method=update``.
        """
        return self._call("adgroups", {"method": "update", "params": {"AdGroups": list(items)}})

    def adimages_get(self) -> dict[str, Any]:
        return self._call("adimages", {"method": "get", "params": {"SelectionCriteria": {}}})

    def creatives_get(self) -> dict[str, Any]:
        return self._call("creatives", {"method": "get", "params": {"SelectionCriteria": {}}})

    def feeds_get(self) -> dict[str, Any]:
        return self._call("feeds", {"method": "get", "params": {"SelectionCriteria": {}}})

    def businesses_get(self) -> dict[str, Any]:
        return self._call("businesses", {"method": "get", "params": {"SelectionCriteria": {}}})

    def agencyclients_get(self) -> dict[str, Any]:
        return self._call("agencyclients", {"method": "get", "params": {"SelectionCriteria": {}}})

    # ------------------------------------------------------------------
    # Live v4 AccountManagement — read-only balance.
    #
    # Live v4 is the legacy POST endpoint at https://api.direct.yandex.ru/
    # live/v4/json/ and uses a different envelope than the v5 JSON-RPC
    # services above: {token, method, param}. The `method` is the
    # service name ("AccountManagement"), and the action is passed
    # inside `param.Action`. We keep this call strictly read-only: we
    # only ever send Action=Get.
    #
    # Auth is also different — Live v4 expects the OAuth token in the
    # JSON body, NOT as an `Authorization: Bearer` header (the v4 server
    # ignores that header). The token is NEVER returned in the result
    # envelope or surfaced in error messages.
    # ------------------------------------------------------------------

    LIVE_V4_BASE_URL = "https://api.direct.yandex.ru/live/v4/json/"

    def account_balance(self, login: str | None = None) -> dict[str, Any]:
        """Read-only Live v4 AccountManagement → Get for the current account.

        Returns ``{"ok": True, "data": [<account block>], "units": ...}`` where
        each ``<account block>`` exposes ``Amount``, ``AmountAvailableForTransfer``,
        ``Currency`` and ``AccountDayBudget`` — the four fields the user
        needs to decide whether the account can keep serving impressions.
        """
        if not self.settings.yandex_oauth_token:
            raise YandexDirectError("YANDEX_OAUTH_TOKEN is required for Yandex Direct API calls")

        body = {
            "token": self.settings.yandex_oauth_token,
            "method": "AccountManagement",
            "param": {
                "Action": "Get",
                "SelectionCriteria": {
                    "Logins": [login] if login else [],
                    "AccountIDS": [],
                },
            },
        }

        try:
            response = self._client.post(
                self.LIVE_V4_BASE_URL,
                json=body,
                headers={"Accept-Language": "ru"},
            )
        except httpx.HTTPError as exc:
            raise YandexDirectError(
                f"Yandex Direct transport error: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            # Do not include headers or body — Live v4 can echo the token
            # or other account identifying data on errors.
            raise YandexDirectError(
                f"Yandex Direct HTTP {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise YandexDirectError("Yandex Direct returned non-JSON response") from exc

        if isinstance(payload, dict) and payload.get("error_code"):
            # Live v4 returns {"error_code": "...", "error_str": "..."} on
            # some failures; surface only the code so the response never
            # echoes the body.
            return {
                "ok": False,
                "error": {"error_code": payload.get("error_code")},
                "units": None,
            }

        if not isinstance(payload, dict):
            raise YandexDirectError("Yandex Direct returned an unexpected Live v4 envelope")

        data_payload = payload.get("data") or []
        data: list[Any]
        if isinstance(data_payload, dict):
            accounts_payload = data_payload.get("Accounts") or data_payload.get("accounts") or []
            if isinstance(accounts_payload, dict):
                data = [accounts_payload]
            elif isinstance(accounts_payload, list):
                data = accounts_payload
            else:
                data = []
        elif isinstance(data_payload, list):
            data = data_payload
        else:
            data = []
        return {"ok": True, "data": data, "units": response.headers.get("Units")}

    # ------------------------------------------------------------------
    # campaigns.get with the finance field set
    #
    # Direct API v5 `campaigns` service supports a wider set of fields
    # than the basic Id/Name/Status/State/Type/DailyBudget set. Adding
    # Funds / Statistics / StartDate / EndDate gives the user a
    # finance-shaped read of every campaign without making a second
    # call. Micro-unit values (1/1_000_000 of currency) are surfaced
    # both as raw ``*_micros`` ints and as display floats, so the
    # caller can pick whichever representation they need.
    # ------------------------------------------------------------------

    _FINANCE_FIELD_NAMES = (
        "Id",
        "Name",
        "Status",
        "State",
        "Type",
        "DailyBudget",
        "Funds",
        "Statistics",
        "StartDate",
        "EndDate",
    )

    @staticmethod
    def _micros_to_display(value: Any) -> float:
        if value is None:
            return 0.0
        try:
            return float(value) / 1_000_000
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _parse_finance_row(cls, campaign: dict[str, Any]) -> dict[str, Any]:
        daily_budget_raw = campaign.get("DailyBudget")
        daily_budget: dict[str, Any]
        if isinstance(daily_budget_raw, dict):
            daily_budget = daily_budget_raw
        else:
            daily_budget = {}
        daily_budget_micros = int(daily_budget.get("Amount") or 0)

        funds_raw = campaign.get("Funds")
        funds: dict[str, Any]
        if isinstance(funds_raw, dict):
            funds = funds_raw
        else:
            funds = {}
        campaign_funds_raw = funds.get("CampaignFunds")
        campaign_funds: dict[str, Any]
        if isinstance(campaign_funds_raw, dict):
            campaign_funds = campaign_funds_raw
        else:
            campaign_funds = {}
        funds_balance_micros = int(campaign_funds.get("Balance") or 0)

        statistics_raw = campaign.get("Statistics")
        statistics: dict[str, Any]
        if isinstance(statistics_raw, dict):
            statistics = statistics_raw
        else:
            statistics = {}
        statistics_shows = int(statistics.get("Shows") or 0)
        statistics_clicks = int(statistics.get("Clicks") or 0)
        # Direct API v5 reports cost in micro-units (1/1_000_000 of currency).
        spend_micros = int(statistics.get("Cost") or 0)

        return {
            "id": str(campaign.get("Id") or ""),
            "name": str(campaign.get("Name") or ""),
            "status": str(campaign.get("Status") or "UNKNOWN"),
            "state": str(campaign.get("State") or "UNKNOWN"),
            "type": str(campaign.get("Type") or "UNKNOWN"),
            "daily_budget_micros": daily_budget_micros,
            "daily_budget": cls._micros_to_display(daily_budget_micros),
            "funds_balance_micros": funds_balance_micros,
            "funds_balance": cls._micros_to_display(funds_balance_micros),
            "spend_micros": spend_micros,
            "spend": cls._micros_to_display(spend_micros),
            "statistics_shows": statistics_shows,
            "statistics_clicks": statistics_clicks,
            "start_date": campaign.get("StartDate"),
            "end_date": campaign.get("EndDate"),
        }

    def campaigns_get_finance(self) -> dict[str, Any]:
        """v5 campaigns.get with the finance-shaped field set.

        Returns ``{"ok": True, "data": [<row>], "units": ...}``. Each row
        surfaces both the raw micro-unit value and the display float for
        money fields so the caller can pick whichever representation
        they need.
        """
        payload = {
            "method": "get",
            "params": {
                "SelectionCriteria": {},
                "FieldNames": list(self._FINANCE_FIELD_NAMES),
            },
        }
        response = self._call("campaigns", payload)
        result_payload = response.get("result")
        if response.get("ok") and isinstance(result_payload, dict):
            raw_campaigns = result_payload.get("Campaigns") or []
            response = {
                **response,
                "data": [self._parse_finance_row(c) for c in raw_campaigns if isinstance(c, dict)],
            }
        return response

    # ------------------------------------------------------------------
    # TimeTargeting read + update
    #
    # Direct API v5 `campaigns` service (see
    # https://yandex.com/dev/direct/doc/ref-v5/campaigns/update.html and
    # .../campaigns/get.html) supports TimeTargeting on a text or
    # dynamic-text campaign. The `TimeTargeting` block is a list of
    # seven `TimeTargetItem` entries, one per day of the week
    # (MONDAY..SUNDAY), each carrying 24 integer `BidPercent` values
    # in the 0..100 range. The `get` shape returns the full block
    # under the campaign's `TimeTargeting` field; the `update` shape
    # accepts the same block under `params.Campaigns[].TimeTargeting`.
    # The `update` call is a REPLACE-shaped call — every other field
    # the operator wants to keep MUST be re-sent. We never invent
    # field names; the helper takes the full `TimeTargeting` list and
    # forwards it.
    # ------------------------------------------------------------------

    _TIME_TARGETING_FIELD_NAMES: tuple[str, ...] = ("Id", "Name", "TimeTargeting")
    _DAILY_BUDGET_FIELD_NAMES: tuple[str, ...] = ("Id", "Name", "DailyBudget")

    def campaigns_get_daily_budget(
        self, campaign_id: int | str
    ) -> dict[str, Any]:
        """v5 ``campaigns.get`` reading the ``DailyBudget`` block.

        Returns the standard ``{ok, result, units, error}`` envelope.
        The ``result.Campaigns[0].DailyBudget`` block (when present)
        carries ``Amount`` (micro-units) and ``SpendMode`` — Direct's
        read-side name for the mode field.  The caller normalises
        ``SpendMode`` → ``Mode`` before including the block in an
        ``campaigns.update`` payload.
        """
        campaign_id = self._direct_id(campaign_id)
        payload = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"Ids": [campaign_id]},
                "FieldNames": list(self._DAILY_BUDGET_FIELD_NAMES),
            },
        }
        return self._call("campaigns", payload)

    def campaigns_get_time_targeting(self, campaign_id: int | str) -> dict[str, Any]:
        """v5 ``campaigns.get`` with the TimeTargeting field set.

        ``campaign_id`` is normalized to an int when it parses as a
        decimal string so the request payload is typed correctly
        (Direct v5 rejects mixed CampaignId types). Non-numeric
        strings are passed through unchanged.

        Returns the standard ``{ok, result, units, error}`` envelope.
        The ``result.Campaigns[0].TimeTargeting`` block is a list of
        seven ``TimeTargetItem`` entries — the exact shape that
        ``campaigns_update_time_targeting`` expects on the apply
        path, so the read-back can be diffed against the applied
        schedule without any reshaping.
        """
        campaign_id = self._direct_id(campaign_id)
        payload = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"Ids": [campaign_id]},
                "FieldNames": list(self._TIME_TARGETING_FIELD_NAMES),
            },
        }
        return self._call("campaigns", payload)

    def campaigns_update_time_targeting(
        self,
        campaign_id: int | str,
        time_targeting: list[dict[str, Any]],
        *,
        daily_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update only the ``TimeTargeting`` block of one campaign.

        ``time_targeting`` MUST be a list of seven ``TimeTargetItem``
        dictionaries, one per day of the week (MONDAY..SUNDAY), each
        carrying the canonical 24 ``BidPercent`` integers in the
        0..100 range. The exact shape the v5 service returns from
        ``campaigns.get TimeTargeting``. We never invent field names.

        ``daily_budget`` is an optional ``DailyBudget`` block to
        include in the ``campaigns.update`` payload. Direct v5
        requires ``DailyBudget.Mode`` when the campaign has a daily
        budget; omitting it yields error_code=8000
        ("Отсутствует обязательный параметр Mode"). The caller reads
        the current ``DailyBudget`` via ``campaigns_get_daily_budget``
        and normalises ``SpendMode`` → ``Mode`` before passing.

        Direct v5 ``campaigns.update`` is a REPLACE-shaped call for
        the ``TimeTargeting`` block — sending it replaces the
        previous schedule atomically. Other campaign fields are
        left untouched (we do not include them in the payload). See
        the v5 docs for the full REPLACE contract.
        """
        if not isinstance(time_targeting, list) or len(time_targeting) != 7:
            # Defensive guard: the store validates the canonical
            # 7 x 24 matrix at the model layer, but a direct
            # caller of this client must also get a typed error if
            # they hand us a malformed schedule.
            raise YandexDirectError(
                "TimeTargeting must be a list of 7 TimeTargetItem entries, "
                f"got {type(time_targeting).__name__} of length "
                f"{len(time_targeting) if hasattr(time_targeting, '__len__') else 'n/a'}"
            )
        campaign_id = self._direct_id(campaign_id)
        campaign_entry: dict[str, Any] = {
            "Id": campaign_id,
            "TimeTargeting": list(time_targeting),
        }
        if daily_budget is not None:
            campaign_entry["DailyBudget"] = dict(daily_budget)
        payload = {
            "method": "update",
            "params": {
                "Campaigns": [campaign_entry]
            },
        }
        return self._call("campaigns", payload)

    # ------------------------------------------------------------------
    # KeywordsResearch — Direct API v5
    #
    # `keywordsresearch` is a real v5 service but only supports two methods
    # at the JSON-RPC level: `hasSearchVolume` and `deduplicate`. Wordstat
    # (create/get/delete) used to live on the older v4 Live URL and is NOT
    # available on the v5 keywordsresearch endpoint, so the wordstat methods
    # here return a safe unsupported envelope and never touch the transport.
    # ------------------------------------------------------------------

    _DEFAULT_HAS_SEARCH_VOLUME_FIELD_NAMES = (
        "Keyword",
        "RegionIds",
        "AllDevices",
        "MobilePhones",
        "Tablets",
        "Desktops",
    )
    _DEFAULT_REGION_IDS: tuple[int, ...] = (43,)

    def keywordsresearch_has_search_volume(
        self,
        keywords: list[str],
        *,
        region_ids: list[int] | None = None,
        field_names: list[str] | None = None,
    ) -> dict[str, Any]:
        params = {
            "SelectionCriteria": {
                "Keywords": keywords,
                "RegionIds": list(region_ids) if region_ids is not None else list(self._DEFAULT_REGION_IDS),
            },
            "FieldNames": list(field_names) if field_names is not None else list(self._DEFAULT_HAS_SEARCH_VOLUME_FIELD_NAMES),
        }
        return self._call("keywordsresearch", {"method": "hasSearchVolume", "params": params})

    @staticmethod
    def _deduplicate_normalize_keywords(keywords: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in keywords:
            if isinstance(item, str):
                normalized.append({"Keyword": item})
            else:
                # Trust caller-provided objects as long as they look like a
                # mapping with at least a `Keyword` field.
                normalized.append(dict(item))
        return normalized

    def keywordsresearch_deduplicate(
        self,
        keywords: list[Any],
        *,
        operation: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "Keywords": self._deduplicate_normalize_keywords(keywords),
        }
        if operation is not None:
            params["Operation"] = operation
        return self._call("keywordsresearch", {"method": "deduplicate", "params": params})

    @staticmethod
    def _unsupported_v5_envelope(method: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "error_code": "UNSUPPORTED_IN_V5",
                "error_detail": (
                    f"{method} is not part of the Direct API v5 keywordsresearch "
                    "service (legacy v4 Live wordstat endpoint). The v5 client "
                    "returns this envelope without making a network call."
                ),
            },
            "units": None,
        }

    def keywordsresearch_create_wordstat_report(
        self, phrases: list[str], geo_ids: list[int]
    ) -> dict[str, Any]:
        return self._unsupported_v5_envelope("createNewWordstatReport")

    def keywordsresearch_get_wordstat_report(self, report_id: int) -> dict[str, Any]:
        return self._unsupported_v5_envelope("getWordstatReport")

    def keywordsresearch_delete_wordstat_report(self, report_id: int) -> dict[str, Any]:
        return self._unsupported_v5_envelope("deleteWordstatReport")

    def report(
        self,
        report_type: str,
        *,
        date_from: str,
        date_to: str,
        field_names: list[str] | None = None,
        campaign_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        field_names = field_names or ["Date", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost", "Ctr"]
        selection_criteria: dict[str, Any] = {"DateFrom": date_from, "DateTo": date_to}
        if campaign_ids:
            # Reports API selection filters campaign ids through Filter items,
            # not through SelectionCriteria.CampaignIds (that shape belongs to
            # many JSON v5 entity services and returns HTTP 400 for reports).
            selection_criteria["Filter"] = [
                {
                    "Field": "CampaignId",
                    "Operator": "IN",
                    "Values": [str(self._direct_id(campaign_id)) for campaign_id in campaign_ids],
                }
            ]
        # Direct reports reject reusing the same ReportName for different
        # parameters. Include a short stable hash of the report definition so
        # changing fields/date/filter does not collide with a queued/generated
        # report of the same type.
        report_signature = {
            "ReportType": report_type,
            "SelectionCriteria": selection_criteria,
            "FieldNames": field_names,
        }
        report_hash = hashlib.sha1(
            json.dumps(report_signature, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:10]
        report_name = f"directpilot-{report_type.lower().replace('_', '-')}-{report_hash}"
        payload = {
            "params": {
                "SelectionCriteria": selection_criteria,
                "FieldNames": field_names,
                "ReportName": report_name,
                "ReportType": report_type,
                "DateRangeType": "CUSTOM_DATE",
                "Format": "TSV",
                "IncludeVAT": "NO",
                "IncludeDiscount": "NO",
            }
        }
        return self._call_report(payload)

    # ------------------------------------------------------------------
    # Limited control adapter (pause / resume)
    #
    # Yandex Direct v5 exposes suspend/resume for campaigns. We keep the
    # adapter intentionally narrow: the service endpoint is "campaigns",
    # method is "suspend" or "resume", and the only parameter is
    # CampaignIds (a list with a single id). See:
    #   https://yandex.com/dev/direct/doc/ref-v5/campaigns/suspend.html
    #   https://yandex.com/dev/direct/doc/ref-v5/campaigns/resume.html
    # ------------------------------------------------------------------

    def suspend_campaign(self, campaign_id: int | str) -> dict[str, Any]:
        return self._control_campaign(campaign_id, "suspend")

    def resume_campaign(self, campaign_id: int | str) -> dict[str, Any]:
        return self._control_campaign(campaign_id, "resume")

    def _control_campaign(self, campaign_id: int | str, method: str) -> dict[str, Any]:
        # Direct API v5 expects CampaignIds as numeric ids. Normalize string
        # ids like "123456" -> 123456 so the request payload is typed
        # correctly. Non-numeric strings are passed through unchanged.
        campaign_id = self._direct_id(campaign_id)
        payload = {
            "method": method,
            "params": {"CampaignIds": [campaign_id]},
        }
        return self._call("campaigns", payload)

    def _call_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.yandex_oauth_token:
            raise YandexDirectError("YANDEX_OAUTH_TOKEN is required for Yandex Direct API calls")

        try:
            response = self._client.post(
                f"{self.base_url}/reports",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.settings.yandex_oauth_token}",
                    "Accept-Language": "ru",
                    "processingMode": "auto",
                    "returnMoneyInMicros": "false",
                    "skipReportHeader": "true",
                    "skipColumnHeader": "false",
                    "skipReportSummary": "true",
                },
            )
        except httpx.HTTPError as exc:
            raise YandexDirectError(
                f"Yandex Direct report transport error: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            raise YandexDirectError(f"Yandex Direct reports HTTP {response.status_code}")

        # Reports usually return TSV, but Direct can still return a JSON error
        # envelope with HTTP 200. Do not let that masquerade as an empty TSV
        # report in parsed endpoints.
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "error" in body:
            error = body.get("error")
            if isinstance(error, dict):
                return {
                    "ok": False,
                    "error": {"error_code": error.get("error_code")},
                    "units": response.headers.get("Units"),
                }
            return {
                "ok": False,
                "error": {"error_code": None},
                "units": response.headers.get("Units"),
            }

        return {"ok": True, "result": response.text, "units": response.headers.get("Units")}

    def _call(self, service: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.yandex_oauth_token:
            raise YandexDirectError("YANDEX_OAUTH_TOKEN is required for Yandex Direct API calls")

        try:
            response = self._client.post(
                f"{self.base_url}/{service}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.settings.yandex_oauth_token}",
                    "Accept-Language": "ru",
                },
            )
        except httpx.HTTPError as exc:
            raise YandexDirectError(
                f"Yandex Direct transport error: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            # Do not include headers or body — body may echo the token back
            # depending on proxy behavior. Surface a redacted message only.
            raise YandexDirectError(
                f"Yandex Direct HTTP {response.status_code}"
            )

        units = response.headers.get("Units")
        try:
            body = response.json()
        except ValueError as exc:
            raise YandexDirectError("Yandex Direct returned non-JSON response") from exc
        if "error" in body:
            # Direct may return body["error"] as a dict (normal) or as a
            # scalar (e.g. a bare string when the proxy short-circuits).
            # Always return a safe envelope that never echoes the raw body
            # — bodies can include tokens, ids, or other identifying data.
            if isinstance(body["error"], dict):
                error = body["error"]
                return {
                    "ok": False,
                    "error": {"error_code": error.get("error_code")},
                    "units": units,
                }
            return {
                "ok": False,
                "error": {
                    "error_code": None,
                    "message": "Yandex Direct returned a non-structured error",
                },
                "units": units,
            }
        return {"ok": True, "result": body.get("result"), "units": units}
