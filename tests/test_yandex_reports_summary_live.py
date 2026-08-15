"""Tests for GET /yandex/reports/summary.

Contract:
- In ``mock`` mode the endpoint MUST fall back to the deterministic mock
  payload and return ``source="mock"``.
- In any non-mock mode (``sandbox``, ``live_readonly``, ``live_write``) AND
  with a configured ``YandexDirectClient``, the endpoint MUST call the real
  ``CAMPAIGN_PERFORMANCE_REPORT`` v5 reports endpoint, parse the TSV response,
  and return ``source="yandex"``, ``read_only=True``, and a ``period`` that
  reflects the requested date range.
- Optional query parameters ``date_from`` / ``date_to`` / ``campaign_id`` are
  accepted without breaking the response model.
- When the live Yandex Direct call raises ``YandexDirectError`` (or returns
  ``ok=False``), the endpoint must surface HTTP 502 with NO OAuth token in
  the response body or detail.
- When the live mode is selected but no client/token is available, the
  endpoint must return HTTP 409 (matches the existing read-only contract
  used by /yandex/campaigns and /yandex/keywords).

Tests use ``httpx.MockTransport`` so no real network call ever happens.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.yandex_direct import YandexDirectClient


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _make_client(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


def _settings_for(mode: str, token: str | None = "t") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=token)


def _install_overrides(settings: Settings, client_obj: YandexDirectClient | None):
    """Install settings + client-factory overrides and return a cleanup fn."""
    from app import main as main_mod

    def _settings_override() -> Settings:
        return settings

    def _client_factory() -> YandexDirectClient | None:
        return client_obj

    app.dependency_overrides[main_mod.get_settings] = _settings_override
    app.dependency_overrides[main_mod.get_yandex_client] = _client_factory

    def cleanup() -> None:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    return cleanup


@pytest.fixture
def client_with_client() -> TestClient:
    yield TestClient(app)


def _campaign_perf_tsv(
    rows: list[tuple[str, str, str, str, str, str, str]] | None = None,
) -> str:
    """Build the server-owned CAMPAIGN summary TSV preset."""
    if rows is None:
        rows = [("2026-06-04", "710691939", "Test campaign", "25", "0", "0", "0.00")]
    fields = [
        "Date", "CampaignId", "CampaignName", "CampaignType", "Impressions", "Clicks",
        "Cost", "Ctr", "AvgCpc", "AvgEffectiveBid", "AvgImpressionPosition",
        "AvgClickPosition", "AvgTrafficVolume", "WeightedImpressions", "WeightedCtr",
        "BounceRate", "AvgPageviews", "Conversions", "ConversionRate", "CostPerConversion",
        "Revenue", "Profit", "GoalsRoi", "PurchaseRevenue", "PurchaseProfit",
        "PurchaseGoalsRoi", "Sessions",
    ]
    lines = ["\t".join(fields)]
    for date_value, campaign_id, campaign_name, impressions, clicks, cost, ctr in rows:
        values = {
            "Date": date_value,
            "CampaignId": campaign_id,
            "CampaignName": campaign_name,
            "CampaignType": "TEXT_CAMPAIGN",
            "Impressions": impressions,
            "Clicks": clicks,
            "Cost": cost,
            "Ctr": ctr,
            "AvgCpc": "0",
            "AvgEffectiveBid": "0",
            "AvgImpressionPosition": "0",
            "AvgClickPosition": "0",
            "AvgTrafficVolume": "0",
            "WeightedImpressions": "0",
            "WeightedCtr": "0",
            "BounceRate": "0",
            "AvgPageviews": "0",
            "Conversions": "0",
            "ConversionRate": "0",
            "CostPerConversion": "0",
            "Revenue": "0",
            "Profit": "0",
            "GoalsRoi": "0",
            "PurchaseRevenue": "0",
            "PurchaseProfit": "0",
            "PurchaseGoalsRoi": "0",
            "Sessions": "0",
        }
        lines.append("\t".join(values[field] for field in fields))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# mock mode → mock fallback
# ---------------------------------------------------------------------------


def test_summary_in_mock_mode_uses_mock_payload(client_with_client: TestClient):
    """In mock mode the endpoint must return source=mock and never touch the
    network. We install a settings override for mock mode and no client."""
    from app import main as main_mod

    settings = _settings_for("mock", token=None)

    def _settings_override() -> Settings:
        return settings

    def _client_factory() -> YandexDirectClient | None:  # always None in mock
        return None

    app.dependency_overrides[main_mod.get_settings] = _settings_override
    app.dependency_overrides[main_mod.get_yandex_client] = _client_factory
    try:
        response = client_with_client.get("/yandex/reports/summary")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "mock"
    assert body["read_only"] is True
    assert body["period"] == "last_7_days"
    assert body["spend"] == 1250.0
    assert body["clicks"] == 42
    assert body["impressions"] == 2100


# ---------------------------------------------------------------------------
# live_readonly + client → real CAMPAIGN_PERFORMANCE_REPORT
# ---------------------------------------------------------------------------


def test_summary_in_live_readonly_calls_campaign_performance_report_and_parses_tsv(
    client_with_client: TestClient,
):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            content=_campaign_perf_tsv(),
            headers={"Units": "RUB", "Content-Type": "text/tab-separated-values"},
        )

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/summary")
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    # Live source / read-only contract
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    # Parsed numbers
    assert body["impressions"] == 25
    assert body["clicks"] == 0
    assert body["spend"] == 0.0
    assert body["ctr"] == 0.0
    # Period reflects the default 7-day range
    assert body["period"] != "last_7_days"
    assert ".." in body["period"]
    # The report endpoint was hit
    assert captured["url"].endswith("/reports")
    body_json = json.dumps(captured["body"])
    assert captured["body"]["params"]["ReportType"] == "CAMPAIGN_PERFORMANCE_REPORT"
    assert captured["body"]["params"]["DateRangeType"] == "CUSTOM_DATE"
    assert "DateFrom" in captured["body"]["params"]["SelectionCriteria"]
    assert "DateTo" in captured["body"]["params"]["SelectionCriteria"]
    # Token must not leak into the response or the captured Authorization.
    assert "LRO-SECRET" not in response.text
    assert captured["authorization"] == "Bearer LRO-SECRET"
    # FieldNames should include the columns the parser depends on.
    fields = captured["body"]["params"]["FieldNames"]
    assert "Impressions" in fields
    assert "Clicks" in fields
    assert "Cost" in fields
    assert "Ctr" in fields


def test_summary_accepts_date_from_and_date_to_query_params(
    client_with_client: TestClient,
):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, content=_campaign_perf_tsv())

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get(
            "/yandex/reports/summary",
            params={"date_from": "2026-06-01", "date_to": "2026-06-07"},
        )
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["period"] == "2026-06-01..2026-06-07"
    assert body["impressions"] == 25
    sel = captured["body"]["params"]["SelectionCriteria"]
    assert sel["DateFrom"] == "2026-06-01"
    assert sel["DateTo"] == "2026-06-07"


def test_summary_accepts_optional_campaign_id_query_param(
    client_with_client: TestClient,
):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, content=_campaign_perf_tsv())

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get(
            "/yandex/reports/summary",
            params={"campaign_id": "710691939"},
        )
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    # Optional campaign_id: query params arrive as strings, but reports API
    # filters by campaign through SelectionCriteria.Filter, not through a
    # CampaignIds field (that shape belongs to many JSON v5 entity services).
    sel = captured["body"]["params"]["SelectionCriteria"]
    assert "CampaignIds" not in sel
    assert sel.get("Filter") == [
        {"Field": "CampaignId", "Operator": "IN", "Values": ["710691939"]}
    ]


def test_summary_live_readonly_without_client_returns_409(
    client_with_client: TestClient,
):
    """Live read mode with a token but no usable client must surface 409.

    This matches the existing read-only contract for /yandex/campaigns and
    /yandex/keywords: a configured token without an instantiated client
    is treated as 'no live Yandex client available', not a silent mock
    fallback (mock fallback is reserved for DIRECTPILOT_MODE=mock).
    """
    settings = _settings_for("live_readonly", token="LRO-SECRET")
    cleanup = _install_overrides(settings, client_obj=None)
    try:
        response = client_with_client.get("/yandex/reports/summary")
    finally:
        cleanup()

    assert response.status_code == 409, response.text
    assert "LRO-SECRET" not in response.text


def test_summary_live_readonly_yandex_error_becomes_502_without_token(
    client_with_client: TestClient,
):
    from app.yandex_direct import YandexDirectError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream boom")

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/summary")
    finally:
        cleanup()

    assert response.status_code == 502, response.text
    assert "LRO-SECRET" not in response.text


def test_summary_sandbox_mode_also_uses_live_report(
    client_with_client: TestClient,
):
    """Sandbox is a non-mock read mode; the live report path must apply too."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, content=_campaign_perf_tsv())

    settings = _settings_for("sandbox", token="SBX-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/summary")
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert captured["url"].startswith("https://api-sandbox.direct.yandex.com/json/v5/reports")
    assert "SBX-SECRET" not in response.text


def test_summary_sums_multiple_rows_in_tsv(client_with_client: TestClient):
    """When the report contains several rows they must be aggregated
    (spend/clicks/impressions summed, CTR/CPC derived from the totals)."""
    tsv = _campaign_perf_tsv(
        rows=[
            ("2026-06-01", "710691939", "Test campaign", "10", "2", "30", "20.00"),
            ("2026-06-02", "710691939", "Test campaign", "15", "1", "15", "6.67"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tsv)

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get(
            "/yandex/reports/summary",
            params={"date_from": "2026-06-01", "date_to": "2026-06-02"},
        )
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["impressions"] == 25
    assert body["clicks"] == 3
    assert body["spend"] == 45.0
    # CTR = clicks / impressions * 100, recomputed from totals
    assert abs(body["ctr"] - 12.0) < 0.01
    # CPC = spend / clicks
    assert abs(body["cpc"] - 15.0) < 0.01
