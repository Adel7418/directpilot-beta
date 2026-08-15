"""Yandex Metrika — read-only client for counters, goals, summary and
traffic-sources.

This is a thin httpx wrapper for the documented Metrika public APIs:

- GET  https://api-metrika.yandex.net/management/v1/counters
- GET  https://api-metrika.yandex.net/management/v1/counter/{id}/goals
- GET  https://api-metrika.yandex.net/stat/v1/data

Auth: ``Authorization: OAuth <token>`` — the documented Metrika way for
service tokens. Bearer / Api-Key are NOT used here.

Safety:
- Missing ``YANDEX_METRIKA_OAUTH_TOKEN`` raises ``YandexMetrikaError``
  *before* any network call.
- HTTP error responses are surfaced as ``YandexMetrikaError`` with only
  the HTTP status code in the message — the response body (which can
  echo the token or include user data) is NEVER included.
- The successful response envelope is ``{"ok": True, "data": <body>}``.

API quirks baked in (these are the documented metrics / dimensions we
use — do not "fix" them):

- The goals-conversion metric is ``ym:s:anyGoalReaches`` — NOT
  ``ym:s:goalReaches`` (the latter is per-goal and is no longer a valid
  metric name in v2).
- The traffic-source dimension is ``ym:s:lastsignTrafficSource`` — NOT
  ``ym:s:TrafficSource`` (the latter is deprecated and breaks in v2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


# Documented Metrika public API base URLs. The management API serves
# counters / goals; the stats API serves the reporting engine.
MANAGEMENT_BASE_URL = "https://api-metrika.yandex.net/management/v1"
STATS_BASE_URL = "https://api-metrika.yandex.net/stat/v1"

# Legacy traffic-sources default. The public API validates the same 1..1000
# range before this value reaches the provider.
_DEFAULT_TRAFFIC_LIMIT = 10

# The server owns every Stats API preset. Public callers choose only a preset
# route and documented bounded options; they never send dimensions, metrics,
# filters, headers, or arbitrary provider parameters.
CORE_SESSION_METRICS = (
    "ym:s:visits",
    "ym:s:users",
    "ym:s:pageviews",
    "ym:s:anyGoalReaches",
    "ym:s:anyGoalConversionRate",
)
ECOMMERCE_CONVERTED_REVENUE_TEMPLATE = "ym:s:ecommerce{currency}ConvertedRevenue"


@dataclass(frozen=True)
class MetrikaReportPreset:
    """Immutable, documented reporting preset sent to ``/stat/v1/data``."""

    name: str
    dimensions: tuple[str, ...]
    uses_limit: bool


METRIKA_REPORT_PRESETS: dict[str, MetrikaReportPreset] = {
    "site-summary": MetrikaReportPreset(
        name="site-summary", dimensions=("ym:s:date",), uses_limit=False
    ),
    "direct-hierarchy": MetrikaReportPreset(
        name="direct-hierarchy",
        dimensions=(
            "ym:s:lastsignDirectClickOrder",
            "ym:s:lastsignDirectBannerGroup",
            "ym:s:lastsignDirectClickBanner",
        ),
        uses_limit=True,
    ),
    "utm-hierarchy": MetrikaReportPreset(
        name="utm-hierarchy",
        dimensions=(
            "ym:s:lastsignUTMSource",
            "ym:s:lastsignUTMMedium",
            "ym:s:lastsignUTMCampaign",
        ),
        uses_limit=True,
    ),
    "landing-pages": MetrikaReportPreset(
        name="landing-pages", dimensions=("ym:s:startURL",), uses_limit=True
    ),
    "traffic-sources": MetrikaReportPreset(
        name="traffic-sources",
        dimensions=("ym:s:lastsignTrafficSource",),
        uses_limit=True,
    ),
}


def metrika_report_metrics(*, view: str, currency: str) -> tuple[str, ...]:
    """Return the fixed session metric bundle for an allowed report view."""

    if view == "core":
        return CORE_SESSION_METRICS
    if view == "ecommerce" and currency in {"RUB", "USD", "EUR", "YND"}:
        return CORE_SESSION_METRICS + (
            ECOMMERCE_CONVERTED_REVENUE_TEMPLATE.format(currency=currency),
        )
    raise ValueError("unsupported Metrika report view or currency")


class YandexMetrikaError(RuntimeError):
    """Raised for any failure of the Metrika client.

    The error message NEVER includes the OAUTH token, the response body,
    or any other secret material. The HTTP status code is included when
    the error came from a server response.
    """


class YandexMetrikaMissingTokenError(YandexMetrikaError):
    """Raised when Metrika credentials are not configured.

    Kept separate from upstream/transport failures so FastAPI handlers
    can map it to HTTP 503 without brittle string matching.
    """


class YandexMetrikaClient:
    """Thin httpx wrapper for the read-only Metrika endpoints."""

    MANAGEMENT_BASE_URL = MANAGEMENT_BASE_URL
    STATS_BASE_URL = STATS_BASE_URL

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        # httpx.Client is the synchronous counterpart used by FastAPI route
        # handlers (which run on a threadpool for blocking calls). The
        # transport is overridable for tests.
        self._client = httpx.Client(timeout=20, transport=transport)

    # ------------------------------------------------------------------
    # public methods
    # ------------------------------------------------------------------

    def list_counters(self) -> dict[str, Any]:
        """GET /management/v1/counters — all counters accessible by the token."""
        response = self._get(f"{self.MANAGEMENT_BASE_URL}/counters")
        return {"ok": True, "data": response}

    def goals(self, counter_id: int | str) -> dict[str, Any]:
        """GET /management/v1/counter/{id}/goals — all goals for one counter."""
        counter_id = self._direct_id(counter_id)
        response = self._get(f"{self.MANAGEMENT_BASE_URL}/counter/{counter_id}/goals")
        return {"ok": True, "data": response}

    def report(
        self,
        counter_id: int | str,
        *,
        date1: str,
        date2: str,
        accuracy: str,
        dimensions: tuple[str, ...],
        metrics: tuple[str, ...],
        sort: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Run one already-validated, server-owned Stats API preset.

        This method intentionally accepts only values selected by the route
        registry. It never consumes public HTTP query parameters directly.
        """
        counter_id = self._direct_id(counter_id)
        params: dict[str, Any] = {
            "ids": str(counter_id),
            "date1": date1,
            "date2": date2,
            "accuracy": accuracy,
            "dimensions": ",".join(dimensions),
            "metrics": ",".join(metrics),
        }
        if sort is not None:
            params["sort"] = sort
        if limit is not None:
            params["limit"] = int(limit)
        response = self._get(f"{self.STATS_BASE_URL}/data", params=params)
        return {"ok": True, "data": response}

    def summary(
        self,
        counter_id: int | str,
        *,
        date1: str,
        date2: str,
    ) -> dict[str, Any]:
        """Legacy summary adapter with the full site-session metric bundle."""
        return self.report(
            counter_id,
            date1=date1,
            date2=date2,
            accuracy="high",
            dimensions=METRIKA_REPORT_PRESETS["site-summary"].dimensions,
            metrics=CORE_SESSION_METRICS,
        )

    def traffic_sources(
        self,
        counter_id: int | str,
        *,
        date1: str,
        date2: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Legacy traffic-source adapter with the full session metric bundle."""
        return self.report(
            counter_id,
            date1=date1,
            date2=date2,
            accuracy="high",
            dimensions=METRIKA_REPORT_PRESETS["traffic-sources"].dimensions,
            metrics=CORE_SESSION_METRICS,
            sort="-ym:s:visits",
            limit=_DEFAULT_TRAFFIC_LIMIT if limit is None else limit,
        )

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    @staticmethod
    def _direct_id(value: int | str) -> int | str:
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"OAuth {self.settings.yandex_metrika_oauth_token}",
            "Accept-Language": "ru",
        }

    def _check_token(self) -> str | None:
        token = self.settings.yandex_metrika_oauth_token
        if not token:
            # Fail closed: never reach the network without credentials.
            raise YandexMetrikaMissingTokenError(
                "YANDEX_METRIKA_OAUTH_TOKEN is required for Yandex Metrika API calls"
            )
        return token

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        self._check_token()
        try:
            response = self._client.get(url, params=params, headers=self._auth_headers())
        except httpx.HTTPError as exc:
            # The transport error message can include hostnames or proxy
            # info; surface only the exception class so we never echo
            # secrets that the transport may have seen.
            raise YandexMetrikaError(
                f"Yandex Metrika transport error: {type(exc).__name__}"
            ) from exc
        return self._parse(response)

    def _post(self, url: str, body: dict[str, Any]) -> Any:
        self._check_token()
        try:
            response = self._client.post(url, json=body, headers=self._auth_headers())
        except httpx.HTTPError as exc:
            raise YandexMetrikaError(
                f"Yandex Metrika transport error: {type(exc).__name__}"
            ) from exc
        return self._parse(response)

    @staticmethod
    def _parse(response: httpx.Response) -> Any:
        if response.status_code >= 400:
            # Do NOT include response.text or response.headers — the body
            # can echo the token or include user query data, and headers
            # can include proxy traces. Surface only the status.
            raise YandexMetrikaError(f"Yandex Metrika HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            # A non-JSON 2xx response is a server bug, not a client error.
            # Surface it as a transport error with no body echo.
            raise YandexMetrikaError("Yandex Metrika returned a non-JSON response") from exc
