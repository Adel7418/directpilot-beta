"""Tests for the campaign strategy read/update endpoints.

``GET /yandex/campaigns/{campaign_id}/strategy`` — read-only,
no write gate, available in all modes.

``POST /yandex/campaigns/{campaign_id}/strategy`` — gated write
path for updating the ``TextCampaign.BiddingStrategy`` block via
v5 ``campaigns.update``. The contract is identical to the existing
time-targeting endpoint:

* ``dry_run=True`` is ALWAYS allowed and NEVER performs a network
  write. The response includes the exact v5 ``campaigns.update``
  payload that WOULD be sent, with ``applied=False``.
* In ``live_readonly`` mode, ``dry_run=False`` is REJECTED before
  any network call (HTTP 409).
* In ``live_write`` mode, ``dry_run=False`` is allowed only when
  ``approved=True`` and a valid ``idempotency_key`` is supplied.
* ``weekly_spend_limit`` and ``bid_ceiling`` are in RUBLES.
* Audit events are recorded. Token leakage is asserted in every test.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.models import YandexStrategyRequest
from app.store import store
from app.yandex_direct import YandexDirectClient, YandexDirectError


client = TestClient(app)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

SECRET_TOKEN = "TOPSECRET-STRATEGY-001"


def _settings(mode: str, token: str | None = SECRET_TOKEN) -> Settings:
    return Settings(
        _env_file=None, directpilot_mode=mode, yandex_oauth_token=token
    )


def _client_with_handler(settings: Settings, handler) -> YandexDirectClient:
    def _transport(request: httpx.Request) -> httpx.Response:
        try:
            return handler(request)
        except AssertionError:
            raise

    transport = httpx.MockTransport(_transport)
    return YandexDirectClient(settings=settings, transport=transport)


def _strategy_get_handler(
    search_type: str = "HIGHEST_POSITION",
    network_type: str = "SERVING_OFF",
    budget_type: str | None = None,
) -> callable:
    """Build a campaigns.get handler returning a deterministic strategy envelope."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "get"
        params = body.get("params") or {}
        if "State" in params.get("FieldNames", []):
            assert "CounterIds" not in params.get("FieldNames", [])
            assert "CounterIds" in params.get("TextCampaignFieldNames", [])
        # Build the TextCampaign block
        search_block: dict[str, Any] = {
            "BiddingStrategyType": search_type,
        }
        if search_type == "WB_MAXIMUM_CONVERSION_RATE":
            wb_block: dict[str, Any] = {
                "GoalId": 567732835,
                "WeeklySpendLimit": 7000000000,
                "BidCeiling": 1500000000,
            }
            if budget_type:
                wb_block["BudgetType"] = budget_type
            search_block["WbMaximumConversionRate"] = wb_block
        network_block: dict[str, Any] = {
            "BiddingStrategyType": network_type,
        }
        strategy = {
            "Search": search_block,
            "Network": network_block,
        }
        campaign = {
            "Id": 710691939,
            "Name": "Test Campaign",
            "Type": "TEXT_CAMPAIGN",
            "State": "ON",
            "Status": "ACCEPTED",
            "DailyBudget": {
                "Amount": 5000000000,
                "SpendMode": "STANDARD",
            },
            "TextCampaign": {
                "BiddingStrategy": strategy,
                "CounterIds": [123456],
            },
        }
        return httpx.Response(
            200,
            json={
                "result": {"Campaigns": [campaign]},
                "units": "1",
            },
        )

    return handler


def _strategy_update_handler(
    success: bool = True,
    error_code: int = 0,
    error_detail: str = "",
    warnings: list[dict] | None = None,
) -> callable:
    """Build a campaigns.update handler."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "update"
        assert "Campaigns" in body["params"]
        if success:
            resp: dict[str, Any] = {"result": {}}
            if warnings:
                resp["warnings"] = warnings
            return httpx.Response(200, json=resp)
        return httpx.Response(
            200,
            json={
                "error": {
                    "error_code": error_code,
                    "error_detail": error_detail,
                }
            },
        )

    return handler


# ---------------------------------------------------------------------------
# GET /yandex/campaigns/{campaign_id}/strategy
# ---------------------------------------------------------------------------


class TestStrategyRead:
    """Read-only endpoint — no write gates, available in all modes."""

    def test_mock_mode_returns_deterministic_data(self):
        """In mock mode, GET strategy returns deterministic mock data."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.get("/yandex/campaigns/710691939/strategy")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "mock"
        assert body["read_only"] is True
        assert body["campaign_type"] == "TEXT_CAMPAIGN"
        assert body["strategy"] is not None
        assert body["strategy"]["Search"]["BiddingStrategyType"] == "HIGHEST_POSITION"
        assert body["strategy"]["Network"]["BiddingStrategyType"] == "SERVING_OFF"
        assert body["strategy_summary"] is not None
        assert body["strategy_summary"]["search"]["type"] == "HIGHEST_POSITION"

    def test_mock_mode_no_token_leakage(self):
        """GET strategy never leaks the token."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.get("/yandex/campaigns/710691939/strategy")
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    def test_live_mode_returns_strategy_from_handler(self):
        """In live_write mode with a handler, returns strategy from the handler."""
        settings = _settings("live_write")
        handler_fn = _strategy_get_handler(
            search_type="WB_MAXIMUM_CONVERSION_RATE",
            network_type="SERVING_OFF",
            budget_type="WEEKLY_BUDGET",
        )
        yandex = _client_with_handler(settings, handler_fn)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.get("/yandex/campaigns/710691939/strategy")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "yandex"
        assert body["read_only"] is True
        assert body["campaign_type"] == "TEXT_CAMPAIGN"
        assert body["state"] == "ON"
        assert body["status"] == "ACCEPTED"
        assert body["counter_ids"] == [123456]
        assert body["daily_budget"] is not None
        assert body["strategy"] is not None
        # Strategy summary should include WB params in rubles
        summary = body["strategy_summary"]
        assert summary["search"]["type"] == "WB_MAXIMUM_CONVERSION_RATE"
        wb = summary["search"]["WbMaximumConversionRate"]
        assert wb["goal_id"] == 567732835
        assert wb["weekly_spend_limit_rub"] == 7000.0
        assert wb["bid_ceiling_rub"] == 1500.0
        assert summary["network"]["type"] == "SERVING_OFF"

    def test_live_mode_no_token_leakage(self):
        """GET strategy in live mode never leaks the token."""
        settings = _settings("live_write")
        handler_fn = _strategy_get_handler()
        yandex = _client_with_handler(settings, handler_fn)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.get("/yandex/campaigns/710691939/strategy")
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    def test_live_mode_surfaces_502_on_error(self):
        """GET strategy surfaces 502 on Yandex error without leaking token."""
        settings = _settings("live_write")

        def error_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": {
                        "error_code": 9999,
                        "error_detail": "Campaign not found",
                    }
                },
            )

        yandex = _client_with_handler(settings, error_handler)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.get("/yandex/campaigns/710691939/strategy")
        assert resp.status_code == 502
        body_text = json.dumps(resp.json())
        assert "9999" in body_text
        assert SECRET_TOKEN not in body_text

    def test_live_mode_without_client_returns_mock_fallback(self):
        """GET strategy with live mode but no client falls back to mock."""
        settings = _settings("live_readonly")
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None

        resp = client.get("/yandex/campaigns/710691939/strategy")
        # Falls through to mock path — same behaviour as GET time-targeting
        assert resp.status_code == 200
        assert resp.json()["source"] == "mock"


# ---------------------------------------------------------------------------
# POST /yandex/campaigns/{campaign_id}/strategy
# ---------------------------------------------------------------------------


class TestStrategyUpdate:
    """Gated write endpoint — dry-run preview vs real apply."""

    BASE_PAYLOAD: dict[str, Any] = {
        "approved": True,
        "idempotency_key": "strat-test-001",
        "dry_run": True,
        "strategy_type": "WB_MAXIMUM_CONVERSION_RATE",
        "goal_id": 567732835,
        "weekly_spend_limit": 7000.0,
        "bid_ceiling": 1500.0,
        "reason": "Switch to conversion strategy",
    }

    def _override(
        self,
        mode: str,
        token: str | None = SECRET_TOKEN,
        get_handler: callable | None = None,
        update_handler: callable | None = None,
    ) -> None:
        settings = _settings(mode, token)
        app.dependency_overrides[get_settings] = lambda: settings

        if get_handler or update_handler:
            # Build a client that routes to the right handler
            def router(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                if body["method"] == "get" and get_handler:
                    return get_handler(request)
                if body["method"] == "update" and update_handler:
                    return update_handler(request)
                return httpx.Response(500, json={"error": "no handler"})

            yandex = _client_with_handler(settings, router)
            app.dependency_overrides[get_yandex_client] = lambda: yandex
        else:
            app.dependency_overrides[get_yandex_client] = lambda: None

    def teardown_method(self):
        app.dependency_overrides.clear()
        # Clear stored results between tests
        store._strategy_results_by_key.clear()

    # --- Model validation ---------------------------------------------------

    def test_missing_approved_is_422(self):
        """POST without approved field is rejected by Pydantic."""
        payload = dict(self.BASE_PAYLOAD)
        del payload["approved"]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    def test_missing_goal_id_is_422(self):
        """POST without goal_id is rejected by Pydantic."""
        payload = dict(self.BASE_PAYLOAD)
        del payload["goal_id"]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    def test_missing_weekly_spend_limit_is_422(self):
        """POST without weekly_spend_limit is rejected by Pydantic."""
        payload = dict(self.BASE_PAYLOAD)
        del payload["weekly_spend_limit"]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    def test_short_idempotency_key_is_422(self):
        """POST with idempotency_key < 6 chars is rejected."""
        payload = dict(self.BASE_PAYLOAD)
        payload["idempotency_key"] = "abc"
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    def test_zero_goal_id_is_422(self):
        """POST with goal_id=0 is rejected."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_id"] = 0
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    def test_negative_weekly_spend_limit_is_422(self):
        """POST with weekly_spend_limit <= 0 is rejected."""
        payload = dict(self.BASE_PAYLOAD)
        payload["weekly_spend_limit"] = -100
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    # --- Unapproved rejection -----------------------------------------------

    def test_approved_false_rejected_409(self):
        """POST with approved=False is rejected with 409."""
        payload = dict(self.BASE_PAYLOAD)
        payload["approved"] = False
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 409
        assert "approval" in resp.json()["detail"].lower()

    # --- Dry-run preview (mock mode) ---------------------------------------

    def test_dry_run_mock_returns_preview(self):
        """dry_run=True in mock mode returns a payload preview."""
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy",
            json=self.BASE_PAYLOAD,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dry_run"] is True
        assert body["applied"] is False
        assert body["source"] == "mock"
        assert body["payload_preview"] is not None
        preview = body["payload_preview"]
        assert preview["method"] == "campaigns.update"
        campaigns = preview["params"]["Campaigns"]
        assert len(campaigns) == 1
        entry = campaigns[0]
        assert "TextCampaign" in entry
        tc = entry["TextCampaign"]
        assert "BiddingStrategy" in tc
        bs = tc["BiddingStrategy"]
        assert bs["Search"]["BiddingStrategyType"] == "WB_MAXIMUM_CONVERSION_RATE"
        wb = bs["Search"]["WbMaximumConversionRate"]
        assert wb["GoalId"] == 567732835
        assert wb["WeeklySpendLimit"] == 7_000_000_000  # 7000 RUB in micros
        assert wb["BidCeiling"] == 1_500_000_000  # 1500 RUB in micros
        assert body["strategy_applied"] is not None
        assert body["strategy_applied"]["search"]["type"] == "WB_MAXIMUM_CONVERSION_RATE"
        assert body["strategy_applied"]["search"]["weekly_spend_limit_rub"] == 7000.0

    def test_dry_run_mock_no_token_leakage(self):
        """dry_run in mock mode never leaks the token."""
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy",
            json=self.BASE_PAYLOAD,
        )
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    # --- Dry-run preview (live mode) ---------------------------------------

    def test_dry_run_live_write_returns_preview(self):
        """dry_run=True in live_write mode returns a live payload preview."""
        get_handler = _strategy_get_handler(
            search_type="WB_MAXIMUM_CONVERSION_RATE",
            budget_type="WEEKLY_BUDGET",
        )
        self._override("live_write", get_handler=get_handler)
        resp = client.post(
            "/yandex/campaigns/710691939/strategy",
            json=self.BASE_PAYLOAD,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dry_run"] is True
        assert body["applied"] is False
        assert body["source"] == "yandex"
        assert body["payload_preview"] is not None

        # BudgetType should be preserved in the preview
        preview = body["payload_preview"]
        campaigns = preview["params"]["Campaigns"]
        entry = campaigns[0]
        bs = entry["TextCampaign"]["BiddingStrategy"]
        wb = bs["Search"]["WbMaximumConversionRate"]
        assert wb.get("BudgetType") == "WEEKLY_BUDGET"

    def test_dry_run_live_no_token_leakage(self):
        """dry_run in live mode never leaks the token."""
        get_handler = _strategy_get_handler()
        self._override("live_write", get_handler=get_handler)
        resp = client.post(
            "/yandex/campaigns/710691939/strategy",
            json=self.BASE_PAYLOAD,
        )
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    # --- live_readonly rejection -------------------------------------------

    def test_dry_run_false_live_readonly_rejected_409(self):
        """dry_run=False in live_readonly is rejected before any network call."""
        get_handler = _strategy_get_handler()
        self._override("live_readonly", get_handler=get_handler)
        payload = dict(self.BASE_PAYLOAD)
        payload["dry_run"] = False
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 409
        assert "live_write" in resp.json()["detail"].lower()

    def test_dry_run_false_sandbox_rejected_409(self):
        """dry_run=False in sandbox is rejected with 409."""
        get_handler = _strategy_get_handler()
        self._override("sandbox", get_handler=get_handler)
        payload = dict(self.BASE_PAYLOAD)
        payload["dry_run"] = False
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 409
        assert "live_write" in resp.json()["detail"].lower()

    def test_dry_run_false_mock_rejected_409(self):
        """dry_run=False in mock is rejected with 409."""
        self._override("mock")
        payload = dict(self.BASE_PAYLOAD)
        payload["dry_run"] = False
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 409

    # --- Idempotency -------------------------------------------------------

    def test_idempotency_replay_returns_cached_result(self):
        """Replay with the same idempotency_key returns cached result."""
        self._override("mock")
        payload = dict(self.BASE_PAYLOAD)

        resp1 = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp1.status_code == 200
        resp2 = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp2.status_code == 200
        # Both responses should be identical
        assert resp1.json() == resp2.json()

    def test_idempotency_different_dry_run_rejected(self):
        """Replay with same key but different dry_run is rejected."""
        self._override("mock")
        payload1 = dict(self.BASE_PAYLOAD)
        payload1["dry_run"] = True
        resp1 = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload1
        )
        assert resp1.status_code == 200

        payload2 = dict(self.BASE_PAYLOAD)
        payload2["dry_run"] = False
        resp2 = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload2
        )
        # Should be rejected — different dry_run on same key
        assert resp2.status_code != 200

    # --- live_write apply (with mock transport) ----------------------------

    def test_apply_live_write_succeeds(self):
        """dry_run=False in live_write applies and returns readback."""
        get_handler = _strategy_get_handler(
            search_type="WB_MAXIMUM_CONVERSION_RATE",
            budget_type="WEEKLY_BUDGET",
        )
        update_handler = _strategy_update_handler(success=True)
        self._override(
            "live_write",
            get_handler=get_handler,
            update_handler=update_handler,
        )

        payload = dict(self.BASE_PAYLOAD)
        payload["dry_run"] = False
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dry_run"] is False
        assert body["applied"] is True
        assert body["source"] == "yandex"
        assert body["readback"] is not None

    def test_apply_live_write_no_token_leakage(self):
        """Real apply never leaks the token."""
        get_handler = _strategy_get_handler(
            search_type="WB_MAXIMUM_CONVERSION_RATE",
            budget_type="WEEKLY_BUDGET",
        )
        update_handler = _strategy_update_handler(success=True)
        self._override(
            "live_write",
            get_handler=get_handler,
            update_handler=update_handler,
        )

        payload = dict(self.BASE_PAYLOAD)
        payload["dry_run"] = False
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    def test_apply_upstream_error_returns_502(self):
        """Upstream Yandex error surfaces as 502 without token."""
        get_handler = _strategy_get_handler(
            search_type="WB_MAXIMUM_CONVERSION_RATE",
            budget_type="WEEKLY_BUDGET",
        )
        update_handler = _strategy_update_handler(
            success=False,
            error_code=8000,
            error_detail="Missing required parameter",
        )
        self._override(
            "live_write",
            get_handler=get_handler,
            update_handler=update_handler,
        )

        payload = dict(self.BASE_PAYLOAD)
        payload["dry_run"] = False
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 502
        body_text = json.dumps(resp.json())
        assert "8000" in body_text
        assert SECRET_TOKEN not in body_text

    # --- BudgetType preservation -------------------------------------------

    def test_budget_type_preserved_in_payload(self):
        """BudgetType from readback is preserved in the write payload."""
        get_handler = _strategy_get_handler(
            search_type="WB_MAXIMUM_CONVERSION_RATE",
            budget_type="WEEKLY_BUDGET",
        )
        self._override("live_write", get_handler=get_handler)

        resp = client.post(
            "/yandex/campaigns/710691939/strategy",
            json=self.BASE_PAYLOAD,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        preview = body["payload_preview"]
        bs = preview["params"]["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]
        wb = bs["Search"]["WbMaximumConversionRate"]
        assert wb.get("BudgetType") == "WEEKLY_BUDGET"

    # --- Network preserve / SERVING_OFF behavior ---------------------------

    def test_network_serving_off_explicit(self):
        """When network=SERVING_OFF, it is set explicitly in the payload."""
        get_handler = _strategy_get_handler(
            network_type="SERVING_OFF",
        )
        self._override("live_write", get_handler=get_handler)

        payload = dict(self.BASE_PAYLOAD)
        payload["network"] = "SERVING_OFF"
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        preview = body["payload_preview"]
        bs = preview["params"]["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]
        assert bs["Network"]["BiddingStrategyType"] == "SERVING_OFF"
        assert body["strategy_applied"]["network"]["type"] == "SERVING_OFF"

    def test_network_preserved_from_readback(self):
        """When network is omitted, the current Network strategy is preserved."""
        get_handler = _strategy_get_handler(
            network_type="SERVING_OFF",
        )
        self._override("live_write", get_handler=get_handler)

        payload = dict(self.BASE_PAYLOAD)
        # network not set — should preserve from readback
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        preview = body["payload_preview"]
        bs = preview["params"]["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]
        # Network should be SERVING_OFF (preserved from handler)
        assert bs["Network"]["BiddingStrategyType"] == "SERVING_OFF"

    # --- Rubles-to-micros conversion ---------------------------------------

    def test_weekly_spend_limit_converted_to_micros(self):
        """weekly_spend_limit in rubles is converted to micros in the payload."""
        self._override("mock")
        payload = dict(self.BASE_PAYLOAD)
        payload["weekly_spend_limit"] = 7000.0

        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        preview = body["payload_preview"]
        bs = preview["params"]["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]
        wb = bs["Search"]["WbMaximumConversionRate"]
        assert wb["WeeklySpendLimit"] == 7_000_000_000  # 7000 * 1M

    def test_bid_ceiling_converted_to_micros(self):
        """bid_ceiling in rubles is converted to micros in the payload."""
        self._override("mock")
        payload = dict(self.BASE_PAYLOAD)
        payload["bid_ceiling"] = 1500.0

        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        preview = body["payload_preview"]
        bs = preview["params"]["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]
        wb = bs["Search"]["WbMaximumConversionRate"]
        assert wb["BidCeiling"] == 1_500_000_000  # 1500 * 1M

    def test_optional_bid_ceiling_omitted(self):
        """When bid_ceiling is not provided, it is not in the payload."""
        self._override("mock")
        payload = dict(self.BASE_PAYLOAD)
        del payload["bid_ceiling"]

        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        preview = body["payload_preview"]
        bs = preview["params"]["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]
        wb = bs["Search"]["WbMaximumConversionRate"]
        assert "BidCeiling" not in wb

    # --- Edge cases --------------------------------------------------------

    def test_daily_budget_null_in_readback(self):
        """When DailyBudget is null, the payload should not include it."""
        def null_budget_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["method"] == "get"
            campaign = {
                "Id": 710691939,
                "Name": "Test Campaign",
                "Type": "TEXT_CAMPAIGN",
                "State": "ON",
                "Status": "ACCEPTED",
                "DailyBudget": None,
                "CounterIds": [123456],
                "TextCampaign": {
                    "BiddingStrategy": {
                        "Search": {
                            "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                            "WbMaximumConversionRate": {
                                "GoalId": 567732835,
                                "WeeklySpendLimit": 7000000000,
                                "BudgetType": "WEEKLY_BUDGET",
                            },
                        },
                        "Network": {
                            "BiddingStrategyType": "SERVING_OFF",
                        },
                    },
                },
            }
            return httpx.Response(
                200,
                json={"result": {"Campaigns": [campaign]}, "units": "1"},
            )

        settings = _settings("live_write")
        app.dependency_overrides[get_settings] = lambda: settings
        yandex = _client_with_handler(settings, null_budget_handler)
        app.dependency_overrides[get_yandex_client] = lambda: yandex

        resp = client.post(
            "/yandex/campaigns/710691939/strategy",
            json=self.BASE_PAYLOAD,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        preview = body["payload_preview"]
        campaigns = preview["params"]["Campaigns"]
        entry = campaigns[0]
        # DailyBudget should not be present when null
        assert "DailyBudget" not in entry

    def test_audit_event_recorded(self):
        """Every POST request records an audit event."""
        self._override("mock")
        before = len(store.audit_events)
        client.post(
            "/yandex/campaigns/710691939/strategy",
            json=self.BASE_PAYLOAD,
        )
        after = len(store.audit_events)
        assert after > before


# ---------------------------------------------------------------------------
# Multi-goal strategy tests
# ---------------------------------------------------------------------------


class TestMultiGoalStrategy:
    """Tests for goal_ids / priority_goals multi-goal optimization."""

    BASE_PAYLOAD: dict[str, Any] = {
        "approved": True,
        "idempotency_key": "multi-goal-001",
        "dry_run": True,
        "strategy_type": "WB_MAXIMUM_CONVERSION_RATE",
        "weekly_spend_limit": 7000.0,
        "bid_ceiling": 1500.0,
    }

    def _override(self, mode: str) -> None:
        settings = _settings(mode)
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: None

    def teardown_method(self):
        app.dependency_overrides.clear()
        store._strategy_results_by_key.clear()

    # --- validation: mutual exclusion -------------------------------------

    def test_goal_ids_and_goal_id_conflict_is_422(self):
        """goal_id + goal_ids together is rejected."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_id"] = 123
        payload["goal_ids"] = [1, 2, 3]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    def test_goal_ids_and_priority_goals_conflict_is_422(self):
        """goal_ids + priority_goals together is rejected."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_ids"] = [1, 2, 3]
        payload["priority_goals"] = [
            {"goal_id": 1, "value": 5.0},
            {"goal_id": 2, "value": 3.0},
        ]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    def test_no_goal_mode_is_422(self):
        """No goal_id, goal_ids, or priority_goals is rejected."""
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy",
            json=self.BASE_PAYLOAD,
        )
        assert resp.status_code == 422

    def test_goal_id_alone_still_works(self):
        """Single goal_id (backward-compatible) still works."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_id"] = 567732835
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["applied"] is False  # dry_run
        preview = body["payload_preview"]
        campaigns = preview["params"]["Campaigns"]
        wb = campaigns[0]["TextCampaign"]["BiddingStrategy"]["Search"][
            "WbMaximumConversionRate"
        ]
        assert wb["GoalId"] == 567732835
        # Single-goal mode explicitly clears PriorityGoals to avoid
        # ambiguous omission semantics on Direct API v5.
        tc = campaigns[0]["TextCampaign"]
        assert "PriorityGoals" in tc
        assert tc["PriorityGoals"]["Items"] == []

    # --- validation: goal_ids ---------------------------------------------

    def test_empty_goal_ids_is_422(self):
        """Empty goal_ids list is rejected."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_ids"] = []
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    def test_goal_ids_duplicates_is_422(self):
        """Duplicate goal ids in goal_ids list is rejected."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_ids"] = [1, 2, 2]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    def test_goal_ids_exceeds_30_is_422(self):
        """More than 30 goal_ids is rejected."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_ids"] = list(range(1, 32))
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    def test_goal_ids_negative_is_422(self):
        """Negative goal_id in goal_ids is rejected."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_ids"] = [1, -2, 3]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    # --- validation: priority_goals ---------------------------------------

    def test_empty_priority_goals_is_422(self):
        """Empty priority_goals list is rejected."""
        payload = dict(self.BASE_PAYLOAD)
        payload["priority_goals"] = []
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    def test_priority_goals_duplicates_is_422(self):
        """Duplicate goal_id in priority_goals is rejected."""
        payload = dict(self.BASE_PAYLOAD)
        payload["priority_goals"] = [
            {"goal_id": 1, "value": 5.0},
            {"goal_id": 1, "value": 3.0},
        ]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 422

    # --- dry-run: goal_ids → GoalId=13 + PriorityGoals --------------------

    def test_dry_run_goal_ids_produces_priority_goals_payload(self):
        """goal_ids produces GoalId=13 + PriorityGoals.Items with equal 1.0 RUB."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_ids"] = [10, 20, 30]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        preview = body["payload_preview"]
        campaigns = preview["params"]["Campaigns"]
        entry = campaigns[0]
        tc = entry["TextCampaign"]

        # PriorityGoals present
        assert "PriorityGoals" in tc
        pg = tc["PriorityGoals"]
        assert "Items" in pg
        assert len(pg["Items"]) == 3

        # Each item has GoalId and Value (in micros = 1.0 RUB × 1_000_000)
        for item, gid in zip(pg["Items"], [10, 20, 30]):
            assert item["GoalId"] == gid
            assert item["Value"] == 1_000_000  # 1.0 RUB in micros

        # GoalId=13 in WbMaximumConversionRate
        wb = tc["BiddingStrategy"]["Search"]["WbMaximumConversionRate"]
        assert wb["GoalId"] == 13

        # strategy_applied includes priority_goals in rubles
        sa = body["strategy_applied"]
        assert sa["search"]["goal_id"] == 13
        assert "priority_goals" in sa
        assert sa["priority_goals"] == [
            {"goal_id": 10, "value_rub": 1.0},
            {"goal_id": 20, "value_rub": 1.0},
            {"goal_id": 30, "value_rub": 1.0},
        ]

    def test_dry_run_priority_goals_explicit_values(self):
        """priority_goals with explicit RUB values → micros in payload."""
        payload = dict(self.BASE_PAYLOAD)
        payload["priority_goals"] = [
            {"goal_id": 100, "value": 10.0},
            {"goal_id": 200, "value": 5.0},
        ]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        preview = body["payload_preview"]
        tc = preview["params"]["Campaigns"][0]["TextCampaign"]
        items = tc["PriorityGoals"]["Items"]
        assert items[0]["GoalId"] == 100
        assert items[0]["Value"] == 10_000_000  # 10 RUB
        assert items[1]["GoalId"] == 200
        assert items[1]["Value"] == 5_000_000  # 5 RUB

        sa = body["strategy_applied"]
        assert sa["priority_goals"] == [
            {"goal_id": 100, "value_rub": 10.0},
            {"goal_id": 200, "value_rub": 5.0},
        ]

    def test_dry_run_priority_goals_none_value_uses_default(self):
        """priority_goals with None value falls back to 1.0 RUB."""
        payload = dict(self.BASE_PAYLOAD)
        payload["priority_goals"] = [
            {"goal_id": 300},
            {"goal_id": 400, "value": 7.0},
        ]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items = body["payload_preview"]["params"]["Campaigns"][0][
            "TextCampaign"
        ]["PriorityGoals"]["Items"]
        assert items[0]["Value"] == 1_000_000  # default 1.0 RUB
        assert items[1]["Value"] == 7_000_000  # explicit 7 RUB

    # --- no token leakage -------------------------------------------------

    def test_multi_goal_no_token_leakage(self):
        """Multi-goal mode never leaks the token."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_ids"] = [10, 20, 30]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    def test_priority_goals_no_token_leakage(self):
        """Explicit priority_goals mode never leaks the token."""
        payload = dict(self.BASE_PAYLOAD)
        payload["priority_goals"] = [
            {"goal_id": 100, "value": 10.0},
        ]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    # --- Network / BudgetType preserved in multi-goal ---------------------

    def test_multi_goal_network_preserved(self):
        """Network strategy is preserved in multi-goal mode."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_ids"] = [10, 20]
        payload["network"] = "SERVING_OFF"
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        bs = body["payload_preview"]["params"]["Campaigns"][0][
            "TextCampaign"
        ]["BiddingStrategy"]
        assert bs["Network"]["BiddingStrategyType"] == "SERVING_OFF"
        assert body["strategy_applied"]["network"]["type"] == "SERVING_OFF"

    # --- Idempotency still works in multi-goal ----------------------------

    def test_multi_goal_idempotency_replay(self):
        """Idempotency replay with multi-goal returns cached result."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_ids"] = [10, 20]
        self._override("mock")
        resp1 = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp1.status_code == 200
        resp2 = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp2.status_code == 200
        assert resp1.json() == resp2.json()

    # --- Audit event recorded ---------------------------------------------

    def test_multi_goal_audit_event_recorded(self):
        """Multi-goal request records an audit event."""
        self._override("mock")
        before = len(store.audit_events)
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_ids"] = [10, 20]
        client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        after = len(store.audit_events)
        assert after > before

    # --- PriorityGoals explicit clearing / preservation --------------------

    def test_single_goal_clears_priority_goals_explicitly(self):
        """Single-goal mode includes PriorityGoals.Items=[] to
        explicitly clear any previously-set multi-goal PriorityGoals.

        Omission semantics are ambiguous on Direct API v5 — the
        payload must be explicit to guarantee clearing.
        """
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_id"] = 567732835
        payload["priority_goals"] = None  # explicit single-goal intent
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        tc = body["payload_preview"]["params"]["Campaigns"][0]["TextCampaign"]

        # PriorityGoals MUST be present with Items=[]
        assert "PriorityGoals" in tc, (
            "Single-goal payload must include PriorityGoals to"
            " explicitly clear multi-goal state"
        )
        pg = tc["PriorityGoals"]
        assert "Items" in pg
        assert pg["Items"] == [], (
            "Single-goal PriorityGoals.Items must be empty list,"
            " not absent — omission semantics are ambiguous"
        )

        # Verify the goal_id is the user's, not the multi-goal constant
        wb = tc["BiddingStrategy"]["Search"]["WbMaximumConversionRate"]
        assert wb["GoalId"] == 567732835

    def test_single_goal_no_token_leakage_with_empty_priority_goals(self):
        """Single-goal with explicit PriorityGoals clearing never
        leaks the token."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_id"] = 567732835
        payload["priority_goals"] = None
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200
        body_text = json.dumps(resp.json())
        assert SECRET_TOKEN not in body_text

    def test_multi_goal_preserves_priority_goals_items(self):
        """Multi-goal mode MUST include PriorityGoals.Items with
        real goals — not an empty list."""
        payload = dict(self.BASE_PAYLOAD)
        payload["goal_ids"] = [10, 20]
        self._override("mock")
        resp = client.post(
            "/yandex/campaigns/710691939/strategy", json=payload
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        tc = body["payload_preview"]["params"]["Campaigns"][0]["TextCampaign"]

        assert "PriorityGoals" in tc
        items = tc["PriorityGoals"]["Items"]
        assert len(items) == 2, (
            "Multi-goal PriorityGoals.Items must contain the goals,"
            " not be empty"
        )
        assert items[0]["GoalId"] == 10
        assert items[1]["GoalId"] == 20


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def teardown_module():
    """Clean up dependency overrides after all tests."""
    app.dependency_overrides.clear()
    store._strategy_results_by_key.clear()
