"""Yandex AI Studio / Search API v2 Wordstat client.

This is the modern, documented v2 path for Yandex Wordstat — it is
deliberately separate from the legacy v4 Live wordstat endpoint and from
the v5 `keywordsresearch` service. The v5 service does not expose wordstat
(createNewWordstatReport / getWordstatReport / deleteWordstatReport) at all,
which is why the older YandexDirectClient returns an explicit
`UNSUPPORTED_IN_V5` envelope for those methods (see
`app/yandex_direct.py::YandexDirectClient`).

API documentation: https://yandex.cloud/en/services/search-api

Endpoints used here (all POST, JSON body, API-key auth):

- POST /v2/wordstat/topRequests       — popular queries containing the phrase
- POST /v2/wordstat/dynamics          — show / abs show per period
- POST /v2/wordstat/regions           — share of impressions by region
- POST /v2/wordstat/getRegionsTree     — region tree (no phrase)

Auth: ``Authorization: Api-Key <key>`` (the modern v2 / AI Studio path),
not OAuth. ``folderId`` is optional and can be overridden per call.

Safety:
- Missing ``YANDEX_SEARCH_API_KEY`` raises ``YandexSearchWordstatError``
  *before* any network call.
- HTTP error responses are surfaced as ``YandexSearchWordstatError`` with
  only the HTTP status code in the message — the response body (which can
  echo the API key or include user query data) is NEVER included.
- The successful response envelope is ``{"ok": True, "data": <body>}``
  so callers can branch on success without inspecting Yandex's raw shape.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings

# v2 / Yandex AI Studio / Search API Wordstat base.
BASE_URL = "https://searchapi.api.cloud.yandex.net/v2/wordstat"

# Conventional defaults: 43 is the Yandex region id for Moscow; 50 phrases
# is the typical cap for "what people also search for" lists.
_DEFAULT_REGION_IDS: tuple[int, ...] = (43,)
_DEFAULT_NUM_PHRASES = 50


class YandexSearchWordstatError(RuntimeError):
    """Raised for any failure of the v2 Wordstat client.

    The error message NEVER includes the API key, the response body, or
    any other secret material. The HTTP status code is included when the
    error came from a server response.
    """


class YandexSearchWordstatMissingKeyError(YandexSearchWordstatError):
    """Raised when Search API credentials are not configured.

    Keep this separate from upstream/transport failures so FastAPI handlers
    can map it to HTTP 503 without brittle string matching.
    """


class YandexSearchWordstatClient:
    """Thin httpx wrapper for the v2 Wordstat endpoints."""

    BASE_URL = BASE_URL

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        # httpx.Client is the synchronous counterpart used by FastAPI route
        # handlers (which run on a threadpool for blocking calls). The
        # transport is overridable for tests.
        self._client = httpx.Client(timeout=20, transport=transport)

    # ------------------------------------------------------------------
    # public methods
    # ------------------------------------------------------------------

    def wordstat_top_requests(
        self,
        phrase: str,
        *,
        region_ids: list[int] | None = None,
        limit: int | None = None,
        devices: list[str] | None = None,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /v2/wordstat/topRequests — popular queries containing phrase."""
        body: dict[str, Any] = {
            "phrase": phrase,
            # Search API v2 schema uses string int64 values for regions and
            # numPhrases even though route callers naturally pass integers.
            "regions": self._stringify_ints(region_ids or list(_DEFAULT_REGION_IDS)),
            "numPhrases": str(limit if limit is not None else _DEFAULT_NUM_PHRASES),
        }
        if devices is not None:
            body["devices"] = devices
        folder = folder_id if folder_id is not None else self.settings.yandex_search_folder_id
        if folder:
            body["folderId"] = folder
        response = self._post("topRequests", body)
        return {"ok": True, "data": response}

    def wordstat_dynamics(
        self,
        phrase: str,
        *,
        period: str,
        date_from: str,
        date_to: str | None = None,
        region_ids: list[int] | None = None,
        devices: list[str] | None = None,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /v2/wordstat/dynamics — show / abs show per period."""
        body: dict[str, Any] = {
            "phrase": phrase,
            "period": period,
            "fromDate": date_from,
            "regions": self._stringify_ints(region_ids or list(_DEFAULT_REGION_IDS)),
        }
        if date_to is not None:
            body["toDate"] = date_to
        if devices is not None:
            body["devices"] = devices
        folder = folder_id if folder_id is not None else self.settings.yandex_search_folder_id
        if folder:
            body["folderId"] = folder
        response = self._post("dynamics", body)
        return {"ok": True, "data": response}

    def wordstat_regions_distribution(
        self,
        phrase: str,
        *,
        region: str = "REGION_ALL",
        devices: list[str] | None = None,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /v2/wordstat/regions — share of impressions by region."""
        body: dict[str, Any] = {"phrase": phrase, "region": region}
        if devices is not None:
            body["devices"] = devices
        folder = folder_id if folder_id is not None else self.settings.yandex_search_folder_id
        if folder:
            body["folderId"] = folder
        response = self._post("regions", body)
        return {"ok": True, "data": response}

    def wordstat_regions_tree(
        self,
        *,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /v2/wordstat/getRegionsTree — region tree, no phrase.

        The body is just ``{"folderId": ...}`` (if a folder is set);
        the API does not accept a ``phrase`` field on this endpoint.
        """
        body: dict[str, Any] = {}
        folder = folder_id if folder_id is not None else self.settings.yandex_search_folder_id
        if folder:
            body["folderId"] = folder
        response = self._post("getRegionsTree", body)
        return {"ok": True, "data": response}

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    @staticmethod
    def _stringify_ints(values: list[int]) -> list[str]:
        return [str(value) for value in values]

    def _post(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        api_key = self.settings.yandex_search_api_key
        if not api_key:
            # Fail closed: never reach the network without credentials.
            raise YandexSearchWordstatMissingKeyError(
                "YANDEX_SEARCH_API_KEY is required for Yandex Search API v2 calls"
            )

        url = f"{self.BASE_URL}/{method}"
        headers = {
            "Authorization": f"Api-Key {api_key}",
            "Accept-Language": "ru",
        }
        try:
            response = self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            # The transport error message can include hostnames or proxy
            # info; surface only the exception class so we never echo
            # secrets that the transport may have seen.
            raise YandexSearchWordstatError(
                f"Yandex Search API v2 transport error: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            # Do NOT include response.text or response.headers — the body
            # can echo the key or include user query data, and headers can
            # include proxy traces. Surface only the status.
            raise YandexSearchWordstatError(
                f"Yandex Search API v2 HTTP {response.status_code}"
            )

        try:
            return response.json()
        except ValueError as exc:
            # A non-JSON 2xx response is a server bug, not a client error.
            # Surface it as a transport error with no body echo.
            raise YandexSearchWordstatError(
                "Yandex Search API v2 returned a non-JSON response"
            ) from exc
