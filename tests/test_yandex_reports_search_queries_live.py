"""Tests for GET /yandex/reports/search-queries.

Contract:
- In ``mock`` mode the endpoint MUST fall back to the deterministic mock
  payload and return ``source="mock"``.
- In any non-mock mode (``sandbox``, ``live_readonly``, ``live_write``) AND
  with a configured ``YandexDirectClient``, the endpoint MUST call the real
  ``SEARCH_QUERY_PERFORMANCE_REPORT`` v5 reports endpoint, parse the TSV
  response, and return ``source="yandex"``, ``read_only=True``, and a
  ``period`` that reflects the requested date range.
- Optional query parameters ``date_from`` / ``date_to`` / ``campaign_id``
  are accepted without breaking the response model.
- The Reports API selection filter MUST use
  ``SelectionCriteria.Filter = [{"Field": "CampaignId", "Operator": "IN",
  "Values": ["..."]}]`` and MUST NOT use ``SelectionCriteria.CampaignIds``
  (that shape returns HTTP 400 for the reports endpoint).
- An empty live report returns ``items=[]`` with ``source="yandex"`` — not
  a 502, not a silent mock fallback.
- When the live Yandex Direct call raises ``YandexDirectError`` (or returns
  ``ok=False``), the endpoint must surface HTTP 502 with NO OAuth token in
  the response body or detail.
- When the live mode is selected but no client/token is available, the
  endpoint must return HTTP 409 (matches the existing read-only contract
  used by /yandex/campaigns and /yandex/reports/summary).

Tests use ``httpx.MockTransport`` so no real network call ever happens.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import _aggregate_search_query_tsv, app
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


def _search_query_tsv(
    rows: list[tuple[str, ...]] | None = None,
    include_campaign_name: bool = True,
) -> str:
    """Build the server-owned SEARCH_QUERY_PERFORMANCE_REPORT core TSV."""
    if rows is None:
        rows = [
            ("ремонт квартир казань", "710691939", "Локальный сервис", "1001", "540", "22", "4.07", "660.00"),
            ("сантехник на дом", "710691939", "Эвристический", "1001", "320", "11", "3.44", "320.00"),
        ]

    normalized_rows: list[tuple[str, ...]] = []
    for row in rows:
        if len(row) == 7 and include_campaign_name:
            query, campaign_id, ad_group_id, impressions, clicks, ctr, cost = row
            normalized_rows.append((query, campaign_id, "Локальный сервис", ad_group_id, impressions, clicks, ctr, cost))
            continue
        if len(row) == 7:
            normalized_rows.append(row)
            continue
        if len(row) != 8:
            raise ValueError(f"Expected 7 or 8 columns, got {len(row)}: {row!r}")
        if not include_campaign_name:
            query, campaign_id, _campaign_name, ad_group_id, impressions, clicks, ctr, cost = row
            normalized_rows.append((query, campaign_id, ad_group_id, impressions, clicks, ctr, cost))
            continue
        normalized_rows.append(row)

    fields = [
        "Date", "Query", "CampaignId", "CampaignName", "CampaignType", "AdGroupId",
        "AdGroupName", "Criterion", "CriterionId", "CriterionType", "MatchedKeyword",
        "MatchType", "Impressions", "Clicks", "Cost", "Ctr", "AvgCpc",
    ]
    lines = ["\t".join(fields)]
    for row in normalized_rows:
        if include_campaign_name:
            query, campaign_id, campaign_name, ad_group_id, impressions, clicks, ctr, cost = row
        else:
            query, campaign_id, ad_group_id, impressions, clicks, ctr, cost = row
            campaign_name = "--"
        values = {
            "Date": "2026-06-01",
            "Query": query,
            "CampaignId": campaign_id,
            "CampaignName": campaign_name,
            "CampaignType": "TEXT_CAMPAIGN",
            "AdGroupId": ad_group_id,
            "AdGroupName": "Test ad group",
            "Criterion": "test criterion",
            "CriterionId": "101",
            "CriterionType": "KEYWORD",
            "MatchedKeyword": "test keyword",
            "MatchType": "EXACT",
            "Impressions": impressions,
            "Clicks": clicks,
            "Cost": cost,
            "Ctr": ctr,
            "AvgCpc": "1.25",
        }
        lines.append("\t".join(values[field] for field in fields))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# mock mode → mock fallback
# ---------------------------------------------------------------------------


def test_search_queries_in_mock_mode_uses_mock_payload(client_with_client: TestClient):
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
        response = client_with_client.get("/yandex/reports/search-queries")
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "mock"
    assert body["read_only"] is True
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert body["period"] == f"{yesterday}..{yesterday}"
    assert body["completed_day"] is True
    # Mock payload has the three legacy fixtures; they remain a deterministic
    # mock for mock mode only and must NEVER appear in live source responses.
    assert len(body["items"]) == 3
    assert body["items"][0]["query"] == "сантехник на дом казань"


def test_search_queries_in_mock_mode_preserves_explicit_period(client_with_client: TestClient):
    settings = _settings_for("mock", token=None)
    cleanup = _install_overrides(settings, client_obj=None)
    try:
        response = client_with_client.get(
            "/yandex/reports/search-queries",
            params={"date_from": "2026-06-01", "date_to": "2026-06-07"},
        )
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["period"] == "2026-06-01..2026-06-07"
    assert body["completed_day"] is True


# ---------------------------------------------------------------------------
# live_readonly + client → real SEARCH_QUERY_PERFORMANCE_REPORT
# ---------------------------------------------------------------------------


def test_search_queries_in_live_readonly_calls_search_query_report_and_parses_tsv(
    client_with_client: TestClient,
):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            content=_search_query_tsv(),
            headers={"Units": "RUB", "Content-Type": "text/tab-separated-values"},
        )

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/search-queries")
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    # Live source / read-only contract
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    # Parsed items — exactly the two rows we put in the TSV, not the
    # legacy mock fixture (сантехник на дом казань / вызов электрика
    # недорого / ремонт квартир под ключ).
    assert len(body["items"]) == 2
    assert body["items"][0]["query"] == "ремонт квартир казань"
    assert body["items"][0]["campaign_id"] == "710691939"
    assert body["items"][0]["campaign_name"] == "Локальный сервис"
    assert body["items"][0]["ad_group_id"] == "1001"
    assert body["items"][0]["cost"] == 660.0
    assert body["items"][0]["impressions"] == 540
    assert body["items"][0]["clicks"] == 22
    assert body["items"][0]["cost"] == 660.0
    assert abs(body["items"][0]["ctr"] - 4.07) < 0.01
    assert body["items"][1]["query"] == "сантехник на дом"
    assert body["items"][1]["campaign_id"] == "710691939"
    assert body["items"][1]["campaign_name"] == "Эвристический"
    assert body["items"][1]["ad_group_id"] == "1001"
    assert body["items"][1]["cost"] == 320.0
    # The mock fixture queries must not leak into a live response.
    for q in ("сантехник на дом казань", "вызов электрика недорого", "ремонт квартир под ключ"):
        for item in body["items"]:
            assert item["query"] != q
    # Period reflects the default 7-day range
    assert body["period"] != "last_7_days"
    assert ".." in body["period"]
    # The report endpoint was hit
    assert captured["url"].endswith("/reports")
    assert captured["body"]["params"]["ReportType"] == "SEARCH_QUERY_PERFORMANCE_REPORT"
    assert captured["body"]["params"]["DateRangeType"] == "CUSTOM_DATE"
    assert "DateFrom" in captured["body"]["params"]["SelectionCriteria"]
    assert "DateTo" in captured["body"]["params"]["SelectionCriteria"]
    # Token must not leak into the response or the captured Authorization.
    assert "LRO-SECRET" not in response.text
    assert captured["authorization"] == "Bearer LRO-SECRET"
    # FieldNames should match the live contract.
    fields = captured["body"]["params"]["FieldNames"]
    assert fields == [
        "Date", "Query", "CampaignId", "CampaignName", "CampaignType", "AdGroupId",
        "AdGroupName", "Criterion", "CriterionId", "CriterionType", "MatchedKeyword",
        "MatchType", "Impressions", "Clicks", "Cost", "Ctr", "AvgCpc",
    ]


@pytest.mark.parametrize(
    "ctr",
    [
        "4,07",
        "1,234",
        "4.07 ",
        "4.07\u00a0",
        "1,234.56",
        "1.234,56",
        "4.07%",
        "4.07e0",
    ],
)
def test_search_query_parser_rejects_non_direct_ctr_tokens(ctr: str):
    tsv = (
        "Query\tCampaignId\tAdGroupId\tImpressions\tClicks\tCtr\tCost\n"
        f"strict-decimal\t710691939\t1001\t540\t22\t{ctr}\t660.00\n"
    )

    assert _aggregate_search_query_tsv(tsv) == []


@pytest.mark.parametrize(
    "cost",
    [
        "660,00",
        "1,234",
        "1496.500",
        "660.00 ",
        "660.00\u00a0",
        "1,234.56",
        "1.234,56",
        "660.00%",
        "660e0",
    ],
)
def test_search_query_parser_keeps_row_with_none_for_malformed_cost(cost: str):
    tsv = (
        "Query\tCampaignId\tAdGroupId\tImpressions\tClicks\tCtr\tCost\n"
        f"strict-cost\t710691939\t1001\t540\t22\t4.07\t{cost}\n"
    )

    items = _aggregate_search_query_tsv(tsv)

    assert len(items) == 1
    assert items[0].ctr == 4.07
    assert items[0].cost is None


def test_search_query_parser_accepts_direct_dot_decimal_ctr_and_cost():
    tsv = (
        "Query\tCampaignId\tAdGroupId\tImpressions\tClicks\tCtr\tCost\n"
        "valid-660\t710691939\t1001\t540\t22\t4.07\t660.00\n"
        "valid-1496\t710691939\t1001\t100\t4\t4.07\t1496.50\n"
    )

    items = _aggregate_search_query_tsv(tsv)

    assert [(item.ctr, item.cost) for item in items] == [(4.07, 660.0), (4.07, 1496.5)]


def test_search_queries_live_raw_endpoint_uses_search_query_field_names(
    client_with_client: TestClient,
):
    """The raw diagnostic endpoint must request actual search-query rows.

    Regression: it previously called SEARCH_QUERY_PERFORMANCE_REPORT with the
    default campaign-summary field set, so Direct returned an empty TSV even
    while the parsed endpoint and Direct UI had real search-query data.
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            content=_search_query_tsv(include_campaign_name=False),
            headers={"Units": "RUB", "Content-Type": "text/tab-separated-values"},
        )

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get(
            "/yandex/reports/search-queries-live",
            params={"date_from": "2026-05-19", "date_to": "2026-06-17"},
        )
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert body["method"] == "SEARCH_QUERY_PERFORMANCE_REPORT"
    assert "Date\tQuery\tCampaignId\tCampaignName" in body["data"]
    assert captured["body"]["params"]["ReportType"] == "SEARCH_QUERY_PERFORMANCE_REPORT"
    assert captured["body"]["params"]["FieldNames"] == [
        "Date", "Query", "CampaignId", "CampaignName", "CampaignType", "AdGroupId",
        "AdGroupName", "Criterion", "CriterionId", "CriterionType", "MatchedKeyword",
        "MatchType", "Impressions", "Clicks", "Cost", "Ctr", "AvgCpc",
    ]


def test_search_queries_empty_live_report_returns_empty_items_with_yandex_source(
    client_with_client: TestClient,
):
    """Empty live report = valid response, NOT a mock fallback and NOT a 502."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="")

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/search-queries")
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert body["items"] == []
    # No mock fixtures should leak into an empty live response.
    assert body["items"] != [
        "сантехник на дом казань",
        "вызов электрика недорого",
        "ремонт квартир под ключ",
    ]


def test_search_queries_empty_live_report_with_only_header_returns_empty_items(
    client_with_client: TestClient,
):
    """A report containing only the column header is also an empty result."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_search_query_tsv(rows=[]))

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/search-queries")
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["items"] == []


def test_search_queries_accepts_date_from_and_date_to_query_params(
    client_with_client: TestClient,
):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, content=_search_query_tsv())

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get(
            "/yandex/reports/search-queries",
            params={"date_from": "2026-06-01", "date_to": "2026-06-07"},
        )
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["period"] == "2026-06-01..2026-06-07"
    sel = captured["body"]["params"]["SelectionCriteria"]
    assert sel["DateFrom"] == "2026-06-01"
    assert sel["DateTo"] == "2026-06-07"


def test_search_queries_accepts_optional_campaign_id_with_filter_not_campaign_ids(
    client_with_client: TestClient,
):
    """Reports API campaign filter MUST use SelectionCriteria.Filter, NOT
    SelectionCriteria.CampaignIds (the latter returns HTTP 400 for the
    reports endpoint)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, content=_search_query_tsv())

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get(
            "/yandex/reports/search-queries",
            params={"campaign_id": "710691939"},
        )
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    sel = captured["body"]["params"]["SelectionCriteria"]
    # The pitfall shape MUST NOT be present.
    assert "CampaignIds" not in sel
    # The correct reports shape MUST be present.
    assert sel.get("Filter") == [
        {"Field": "CampaignId", "Operator": "IN", "Values": ["710691939"]}
    ]


def test_search_queries_campaign_id_filter_drops_non_matching_rows(
    client_with_client: TestClient,
):
    """Rows whose CampaignId does not match the campaign_id filter are
    dropped from the parsed result (server-side TSV filter is a hint,
    client-side defence is the source of truth)."""
    tsv = _search_query_tsv(
        rows=[
            ("ремонт квартир казань", "710691939", "1001", "540", "22", "4.07", "660.00"),
            ("чужой запрос", "999999999", "2002", "100", "1", "1.00", "10.00"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tsv)

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get(
            "/yandex/reports/search-queries",
            params={"campaign_id": "710691939"},
        )
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    queries = [item["query"] for item in body["items"]]
    assert "ремонт квартир казань" in queries
    assert "чужой запрос" not in queries


def test_search_queries_campaign_id_filter_works_for_specific_report_ids(
    client_with_client: TestClient,
):
    """Regression coverage for known report campaign IDs with normalized filtering."""
    tsv = _search_query_tsv(
        rows=[
            ("ремонт 1", "710691939", "1111", "10", "1", "10.00", "100.00"),
            ("ремонт 2", " 710382063 ", "2222", "20", "2", "10.00", "200.00"),
            ("ремонт 3", "706306618", "3333", "30", "3", "10.00", "300.00"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tsv)

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        cases = [
            ("710691939", ["ремонт 1"]),
            ("710382063", ["ремонт 2"]),
            ("706306618", ["ремонт 3"]),
        ]
        for campaign_id, expected_queries in cases:
            response = client_with_client.get(
                "/yandex/reports/search-queries",
                params={"campaign_id": campaign_id},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert [item["query"] for item in body["items"]] == expected_queries
            assert all(
                item["campaign_id"] == campaign_id for item in body["items"]
            )
    finally:
        cleanup()


def test_search_queries_campaign_name_fallbacks_to_campaigns_get_when_missing(
    client_with_client: TestClient,
):
    """If CampaignName is missing in TSV rows, resolve it from campaigns.get."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Campaigns": [
                            {"Id": "710691939", "Name": "Локальный сервис"},
                            {"Id": "710382063", "Name": "Эвристический"},
                        ]
                    },
                    "ok": True,
                },
            )

        report_payload = _search_query_tsv(
            rows=[
                ("ремонт 1", "710691939", "1001", "10", "1", "10.00", "100.00"),
                ("ремонт 2", "710382063", "1002", "20", "2", "10.00", "200.00"),
            ],
            include_campaign_name=False,
        )

        return httpx.Response(200, content=report_payload)

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/search-queries")
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    items = body["items"]
    assert len(items) == 2
    assert items[0]["campaign_name"] in {"Локальный сервис", "Эвристический"}
    assert items[1]["campaign_name"] in {"Локальный сервис", "Эвристический"}


def test_search_queries_live_readonly_without_client_returns_409(
    client_with_client: TestClient,
):
    """Live read mode with a token but no usable client must surface 409.

    Matches the existing read-only contract for /yandex/campaigns,
    /yandex/keywords and /yandex/reports/summary: a configured token
    without an instantiated client is treated as 'no live Yandex client
    available', not a silent mock fallback (mock fallback is reserved
    for DIRECTPILOT_MODE=mock)."""
    settings = _settings_for("live_readonly", token="LRO-SECRET")
    cleanup = _install_overrides(settings, client_obj=None)
    try:
        response = client_with_client.get("/yandex/reports/search-queries")
    finally:
        cleanup()

    assert response.status_code == 409, response.text
    assert "LRO-SECRET" not in response.text


def test_search_queries_live_readonly_yandex_transport_error_becomes_502(
    client_with_client: TestClient,
):
    """A transport-level error (HTTP 502 from upstream) surfaces as 502
    with no OAuth token in the response body or detail."""
    from app.yandex_direct import YandexDirectError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream boom")

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/search-queries")
    finally:
        cleanup()

    assert response.status_code == 502, response.text
    assert "LRO-SECRET" not in response.text
    assert "YandexDirectError" in response.text


def test_search_queries_live_readonly_envelope_error_becomes_502(
    client_with_client: TestClient,
):
    """A 200 response carrying a v5-style JSON error envelope (Direct
    can return this in rare cases even with HTTP 200) is also surfaced
    as 502 — never as a silently-parsed empty TSV result."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 52, "error_detail": "Token is invalid"}},
        )

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/search-queries")
    finally:
        cleanup()

    assert response.status_code == 502, response.text
    assert "LRO-SECRET" not in response.text


def test_search_queries_live_readonly_missing_token_raises_yandex_direct_error(
    client_with_client: TestClient,
):
    """A live read mode with no YANDEX_OAUTH_TOKEN must surface 502 (the
    client raises YandexDirectError before any HTTP call is made).

    This is the contract for the 'creds present but unusable' case: the
    handler does NOT fall back to mock data."""
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        return httpx.Response(200, content="")

    settings = _settings_for("live_readonly", token=None)
    # Force a missing token by constructing the client directly; the
    # production get_yandex_client dependency would have raised earlier.
    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    # Drop the token to simulate a broken config (the constructor does not
    # require it; only the call does).
    client_obj.settings.yandex_oauth_token = None
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/search-queries")
    finally:
        cleanup()

    assert response.status_code == 502, response.text
    # 502 detail must be the redacted YandexDirectError message — never
    # include the OAuth token (there is none, but this is the contract
    # we want to lock in).
    assert "YandexDirectError" in response.text


def test_search_queries_live_readonly_malformed_tsv_rows_are_skipped_silently(
    client_with_client: TestClient,
):
    """Malformed rows (missing columns or non-numeric values) are skipped
    silently, not surfaced as 502 — the report body itself is valid."""
    header = _search_query_tsv(rows=[]).splitlines()[0]
    valid_row = _search_query_tsv(
        rows=[("ремонт", "710691939", "1001", "100", "5", "5.00", "150.00")]
    ).splitlines()[1]
    invalid_row = _search_query_tsv(
        rows=[("электрик", "710691939", "1001", "not_a_number", "1", "2.00", "30.00")]
    ).splitlines()[1]
    tsv = f"{header}\n{valid_row}\nсантехник\t710691939\n{invalid_row}\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tsv)

    settings = _settings_for("live_readonly", token="LRO-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/search-queries")
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    # Only the well-formed row survives.
    queries = [item["query"] for item in body["items"]]
    assert queries == ["ремонт"]


def test_search_queries_sandbox_mode_also_uses_live_report(
    client_with_client: TestClient,
):
    """Sandbox is a non-mock read mode; the live report path must apply too."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, content=_search_query_tsv())

    settings = _settings_for("sandbox", token="SBX-SECRET")
    client_obj = _make_client(settings, handler)
    cleanup = _install_overrides(settings, client_obj)
    try:
        response = client_with_client.get("/yandex/reports/search-queries")
    finally:
        cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "yandex"
    assert body["read_only"] is True
    assert captured["url"].startswith(
        "https://api-sandbox.direct.yandex.com/json/v5/reports"
    )
    assert "SBX-SECRET" not in response.text
