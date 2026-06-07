from __future__ import annotations

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
    # Read-only child entities (adgroups / ads / keywords)
    #
    # All three are GET methods on Direct API v5. They accept
    # SelectionCriteria.CampaignIds and return their result objects wrapped
    # in the same {ok, result, units, error} envelope as campaigns_get.
    #

    def adgroups_get(self, campaign_id: int | str) -> dict[str, Any]:
        campaign_id = self._direct_id(campaign_id)
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
        return self._call("vcards", {"method": "get", "params": {"SelectionCriteria": {}}})

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

    def keywordsresearch_has_search_volume(self, keywords: list[str]) -> dict[str, Any]:
        return self._call("keywordsresearch", {"method": "hasSearchVolume", "params": {"Keywords": keywords}})

    def keywordsresearch_deduplicate(self, keywords: list[str]) -> dict[str, Any]:
        return self._call("keywordsresearch", {"method": "deduplicate", "params": {"Keywords": keywords}})

    def keywordsresearch_create_wordstat_report(self, phrases: list[str], geo_ids: list[int]) -> dict[str, Any]:
        return self._call(
            "keywordsresearch",
            {"method": "createNewWordstatReport", "params": {"Phrases": phrases, "GeoID": geo_ids}},
        )

    def keywordsresearch_get_wordstat_report(self, report_id: int) -> dict[str, Any]:
        return self._call("keywordsresearch", {"method": "getWordstatReport", "params": {"ReportID": report_id}})

    def keywordsresearch_delete_wordstat_report(self, report_id: int) -> dict[str, Any]:
        return self._call("keywordsresearch", {"method": "deleteWordstatReport", "params": {"ReportID": report_id}})

    def report(
        self,
        report_type: str,
        *,
        date_from: str,
        date_to: str,
        field_names: list[str] | None = None,
    ) -> dict[str, Any]:
        field_names = field_names or ["Date", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost", "Ctr"]
        report_name = f"directpilot-{report_type.lower().replace('_', '-')}"
        payload = {
            "params": {
                "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
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
                    "error_detail": str(body["error"]),
                },
                "units": units,
            }
        return {"ok": True, "result": body.get("result"), "units": units}
