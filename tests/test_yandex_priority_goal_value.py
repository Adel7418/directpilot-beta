"""Tests for safe updates of an existing manual-strategy priority goal value."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.store import store
from app.yandex_direct import YandexDirectClient


client = TestClient(app)


def _settings(mode: str) -> Settings:
    return Settings(
        _env_file=None,
        directpilot_mode=mode,
        yandex_oauth_token="test-token",
    )


def _client_with_handler(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
    )


def teardown_function() -> None:
    app.dependency_overrides.clear()
    store._priority_goal_value_results_by_key.clear()


def test_manual_highest_position_dry_run_updates_only_selected_goal_value() -> None:
    """Preview preserves manual search, network, budget, and other goals."""
    settings = _settings("live_write")
    calls: list[dict[str, Any]] = []
    campaign = {
        "Id": 710382063,
        "Type": "TEXT_CAMPAIGN",
        "DailyBudget": {"Amount": 1_500_000_000, "Mode": "STANDARD"},
        "TextCampaign": {
            "CounterIds": [123456],
            "BiddingStrategy": {
                "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            },
            "PriorityGoals": {
                "Items": [
                    {"GoalId": 516513575, "Value": 600_000_000, "Operation": "SET"},
                    {"GoalId": 516513576, "Value": 125_000_000, "Operation": "SET"},
                ]
            },
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert body["method"] == "get"
        return httpx.Response(
            200,
            json={"result": {"Campaigns": [campaign]}, "units": "1"},
        )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _client_with_handler(
        settings, handler
    )

    response = client.post(
        "/yandex/campaigns/710382063/priority-goals",
        json={
            "goal_id": 516513575,
            "value_rub": 700.0,
            "dry_run": True,
            "approved": True,
            "idempotency_key": "priority-goal-preview-001",
            "reason": "Increase priority goal value",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is False
    assert body["dry_run"] is True
    assert body["before_value_rub"] == 600.0
    assert body["after_value_rub"] == 700.0
    assert body["preserved_fields"] == [
        "TextCampaign.BiddingStrategy.Search",
        "TextCampaign.BiddingStrategy.Network",
        "TextCampaign.CounterIds",
        "DailyBudget",
        "TextCampaign.PriorityGoals.Items[GoalId=516513576]",
    ]

    preview = body["payload_preview"]
    entry = preview["params"]["Campaigns"][0]
    assert entry["Id"] == 710382063
    assert entry["DailyBudget"] == {"Amount": 1_500_000_000, "Mode": "STANDARD"}
    assert entry["TextCampaign"]["CounterIds"] == [123456]
    assert entry["TextCampaign"]["BiddingStrategy"] == campaign["TextCampaign"][
        "BiddingStrategy"
    ]
    assert entry["TextCampaign"]["PriorityGoals"]["Items"] == [
        {"GoalId": 516513575, "Value": 700_000_000, "Operation": "SET"},
        {"GoalId": 516513576, "Value": 125_000_000, "Operation": "SET"},
    ]
    assert [call["method"] for call in calls] == ["get"]


def test_apply_writes_documented_shape_and_verifies_all_invariants() -> None:
    """Apply sends one update then accepts only an invariant-preserving readback."""
    settings = _settings("live_write")
    calls: list[dict[str, Any]] = []
    before = {
        "Id": 710382063,
        "Type": "TEXT_CAMPAIGN",
        "DailyBudget": {"Amount": 1_500_000_000, "Mode": "STANDARD"},
        "TextCampaign": {
            "CounterIds": [123456],
            "BiddingStrategy": {
                "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            },
            "PriorityGoals": {
                "Items": [
                    {"GoalId": 516513575, "Value": 600_000_000},
                    {"GoalId": 516513576, "Value": 125_000_000},
                ]
            },
        },
    }
    after = {
        **before,
        "TextCampaign": {
            **before["TextCampaign"],
            "PriorityGoals": {
                "Items": [
                    {"GoalId": 516513575, "Value": 700_000_000},
                    {"GoalId": 516513576, "Value": 125_000_000},
                ]
            },
        },
    }
    get_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_calls
        body = json.loads(request.content)
        calls.append(body)
        if body["method"] == "get":
            get_calls += 1
            campaign = before if get_calls == 1 else after
            return httpx.Response(
                200,
                json={"result": {"Campaigns": [campaign]}, "units": "1"},
            )
        assert body["method"] == "update"
        entry = body["params"]["Campaigns"][0]
        assert entry["Id"] == 710382063
        assert entry["DailyBudget"] == before["DailyBudget"]
        assert entry["TextCampaign"]["CounterIds"] == [123456]
        assert entry["TextCampaign"]["BiddingStrategy"] == before["TextCampaign"][
            "BiddingStrategy"
        ]
        assert entry["TextCampaign"]["PriorityGoals"]["Items"] == [
            {"GoalId": 516513575, "Value": 700_000_000, "Operation": "SET"},
            {"GoalId": 516513576, "Value": 125_000_000, "Operation": "SET"},
        ]
        return httpx.Response(
            200,
            json={"result": {"UpdateResults": [{"Id": 710382063}]}},
            headers={"Units": "3"},
        )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _client_with_handler(
        settings, handler
    )

    response = client.post(
        "/yandex/campaigns/710382063/priority-goals",
        json={
            "goal_id": 516513575,
            "value_rub": 700.0,
            "dry_run": False,
            "approved": True,
            "idempotency_key": "priority-goal-apply-001",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["payload_preview"] is None
    assert body["yandex_units"] == 3
    assert body["readback"] == {
        "DailyBudget": {"Amount": 1_500_000_000, "Mode": "STANDARD"},
        "TextCampaign": {
            "CounterIds": [123456],
            "BiddingStrategy": before["TextCampaign"]["BiddingStrategy"],
            "PriorityGoals": {
                "Items": [
                    {"GoalId": 516513575, "Value": 700_000_000},
                    {"GoalId": 516513576, "Value": 125_000_000},
                ]
            },
        },
    }
    assert [call["method"] for call in calls] == ["get", "update", "get"]


def test_auto_strategy_is_rejected_before_campaigns_update() -> None:
    """An automatic strategy must never be changed through this endpoint."""
    settings = _settings("live_write")
    calls: list[str] = []
    campaign = {
        "Id": 710382063,
        "Type": "TEXT_CAMPAIGN",
        "DailyBudget": None,
        "TextCampaign": {
            "CounterIds": [123456],
            "BiddingStrategy": {
                "Search": {
                    "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                    "WbMaximumConversionRate": {
                        "GoalId": 13,
                        "WeeklySpendLimit": 7_000_000_000,
                        "BudgetType": "WEEKLY_BUDGET",
                    },
                },
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            },
            "PriorityGoals": {"Items": [{"GoalId": 516513575, "Value": 600_000_000}]},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["method"])
        assert body["method"] == "get"
        return httpx.Response(200, json={"result": {"Campaigns": [campaign]}})

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _client_with_handler(
        settings, handler
    )

    response = client.post(
        "/yandex/campaigns/710382063/priority-goals",
        json={
            "goal_id": 516513575,
            "value_rub": 700.0,
            "dry_run": False,
            "approved": True,
            "idempotency_key": "priority-goal-auto-001",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "INCOMPATIBLE_CAMPAIGN_STRATEGY"
    assert calls == ["get"]


def test_missing_selected_goal_fails_closed_before_campaigns_update() -> None:
    """The endpoint cannot create a missing priority goal as a side effect."""
    settings = _settings("live_write")
    calls: list[str] = []
    campaign = {
        "Id": 710382063,
        "Type": "TEXT_CAMPAIGN",
        "DailyBudget": {"Amount": 1_500_000_000, "Mode": "STANDARD"},
        "TextCampaign": {
            "CounterIds": [123456],
            "BiddingStrategy": {
                "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            },
            "PriorityGoals": {"Items": [{"GoalId": 516513576, "Value": 125_000_000}]},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["method"])
        assert body["method"] == "get"
        return httpx.Response(200, json={"result": {"Campaigns": [campaign]}})

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _client_with_handler(
        settings, handler
    )
    response = client.post(
        "/yandex/campaigns/710382063/priority-goals",
        json={
            "goal_id": 516513575,
            "value_rub": 700.0,
            "dry_run": False,
            "approved": True,
            "idempotency_key": "priority-goal-missing-001",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "PRIORITY_GOAL_NOT_FOUND"
    assert calls == ["get"]


def test_apply_safety_gates_block_before_any_yandex_call() -> None:
    """Approval, idempotency, and live_write are checked before the reader/writer."""
    calls: list[str] = []

    def forbidden_handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["method"])
        raise AssertionError("Yandex must not be called when a safety gate fails")

    settings = _settings("live_readonly")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _client_with_handler(
        settings, forbidden_handler
    )
    base = {
        "goal_id": 516513575,
        "value_rub": 700.0,
        "dry_run": False,
        "approved": True,
        "idempotency_key": "priority-goal-gate-001",
    }

    readonly = client.post(
        "/yandex/campaigns/710382063/priority-goals", json=base
    )
    assert readonly.status_code == 409
    assert readonly.json()["detail"]["error_code"] == "LIVE_WRITE_REQUIRED"

    unapproved = client.post(
        "/yandex/campaigns/710382063/priority-goals",
        json={**base, "approved": False, "idempotency_key": "priority-goal-gate-002"},
    )
    assert unapproved.status_code == 409
    assert unapproved.json()["detail"]["error_code"] == "APPROVAL_REQUIRED"

    missing_key = client.post(
        "/yandex/campaigns/710382063/priority-goals",
        json={key: value for key, value in base.items() if key != "idempotency_key"},
    )
    assert missing_key.status_code == 422
    assert calls == []


def _manual_priority_goal_campaign(daily_budget: Any) -> dict[str, Any]:
    return {
        "Id": 710382063,
        "Type": "TEXT_CAMPAIGN",
        "DailyBudget": daily_budget,
        "TextCampaign": {
            "CounterIds": [123456],
            "BiddingStrategy": {
                "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            },
            "PriorityGoals": {"Items": [{"GoalId": 516513575, "Value": 600_000_000}]},
        },
    }


@pytest.mark.parametrize(
    "daily_budget",
    [None, {"Amount": 1_500_000_000}, {"Amount": "invalid", "Mode": "STANDARD"}],
)
def test_null_or_malformed_manual_budget_refuses_before_writer(
    daily_budget: Any,
) -> None:
    """Never invent a daily-budget mode for an unsupported readback shape."""
    settings = _settings("live_write")
    calls: list[str] = []
    campaign = _manual_priority_goal_campaign(daily_budget)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["method"])
        assert body["method"] == "get"
        return httpx.Response(200, json={"result": {"Campaigns": [campaign]}})

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _client_with_handler(
        settings, handler
    )
    response = client.post(
        "/yandex/campaigns/710382063/priority-goals",
        json={
            "goal_id": 516513575,
            "value_rub": 700.0,
            "dry_run": False,
            "approved": True,
            "idempotency_key": "priority-goal-budget-001",
        },
    )

    assert response.status_code == 502
    assert calls == ["get"]


@pytest.mark.parametrize(
    ("value_rub", "expected_micros"),
    [(700.000001, 700_000_001), (0.000001, 1)],
)
def test_exact_rubles_convert_to_direct_micros_without_float_loss(
    value_rub: float, expected_micros: int
) -> None:
    """A numeric value at micro precision is preserved exactly in preview."""
    settings = _settings("live_write")
    campaign = _manual_priority_goal_campaign(
        {"Amount": 1_500_000_000, "Mode": "STANDARD"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["method"] == "get"
        return httpx.Response(200, json={"result": {"Campaigns": [campaign]}})

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _client_with_handler(
        settings, handler
    )
    response = client.post(
        "/yandex/campaigns/710382063/priority-goals",
        json={
            "goal_id": 516513575,
            "value_rub": value_rub,
            "dry_run": True,
            "approved": True,
            "idempotency_key": f"priority-goal-micros-{expected_micros}",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["after_value_micros"] == expected_micros
    item = body["payload_preview"]["params"]["Campaigns"][0]["TextCampaign"][
        "PriorityGoals"
    ]["Items"][0]
    assert item["Value"] == expected_micros


@pytest.mark.parametrize("value_rub", [0, -1, 700.0000001, "700.0", True])
def test_priority_goal_value_requires_positive_exact_json_number(value_rub: Any) -> None:
    """Invalid amounts are rejected by request validation before a provider call."""
    app.dependency_overrides[get_settings] = lambda: _settings("live_write")
    app.dependency_overrides[get_yandex_client] = lambda: None
    response = client.post(
        "/yandex/campaigns/710382063/priority-goals",
        json={
            "goal_id": 516513575,
            "value_rub": value_rub,
            "dry_run": True,
            "approved": True,
            "idempotency_key": "priority-goal-invalid-value-001",
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "update_json",
    [
        {"result": {"UpdateResults": [{"Id": 710382063, "Errors": [{"Code": 42}]}]}},
        {"result": {}},
    ],
)
def test_per_item_or_malformed_http_200_update_fails_without_retry(
    update_json: dict[str, Any],
) -> None:
    """A 200 response is not treated as success without a valid per-item result."""
    settings = _settings("live_write")
    calls: list[str] = []
    campaign = _manual_priority_goal_campaign(
        {"Amount": 1_500_000_000, "Mode": "STANDARD"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "get":
            return httpx.Response(200, json={"result": {"Campaigns": [campaign]}})
        assert body["method"] == "update"
        return httpx.Response(200, json=update_json)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _client_with_handler(
        settings, handler
    )
    response = client.post(
        "/yandex/campaigns/710382063/priority-goals",
        json={
            "goal_id": 516513575,
            "value_rub": 700.0,
            "dry_run": False,
            "approved": True,
            "idempotency_key": "priority-goal-provider-error-001",
        },
    )

    assert response.status_code == 502
    assert calls == ["get", "update"]


def test_readback_mismatch_fails_without_success_or_retry() -> None:
    """Apply never reports success when the selected value is not read back."""
    settings = _settings("live_write")
    calls: list[str] = []
    before = _manual_priority_goal_campaign(
        {"Amount": 1_500_000_000, "Mode": "STANDARD"}
    )
    get_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_calls
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "get":
            get_calls += 1
            return httpx.Response(200, json={"result": {"Campaigns": [before]}})
        assert body["method"] == "update"
        return httpx.Response(
            200,
            json={"result": {"UpdateResults": [{"Id": 710382063}]}},
        )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _client_with_handler(
        settings, handler
    )
    response = client.post(
        "/yandex/campaigns/710382063/priority-goals",
        json={
            "goal_id": 516513575,
            "value_rub": 700.0,
            "dry_run": False,
            "approved": True,
            "idempotency_key": "priority-goal-readback-mismatch-001",
        },
    )

    assert response.status_code == 502
    assert calls == ["get", "update", "get"]


def test_idempotent_apply_replay_does_not_dispatch_another_update() -> None:
    """A successful apply is cached by campaign and idempotency key."""
    settings = _settings("live_write")
    calls: list[str] = []
    before = _manual_priority_goal_campaign(
        {"Amount": 1_500_000_000, "Mode": "STANDARD"}
    )
    after = {
        **before,
        "TextCampaign": {
            **before["TextCampaign"],
            "PriorityGoals": {"Items": [{"GoalId": 516513575, "Value": 700_000_000}]},
        },
    }
    get_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_calls
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "get":
            get_calls += 1
            campaign = before if get_calls == 1 else after
            return httpx.Response(200, json={"result": {"Campaigns": [campaign]}})
        assert body["method"] == "update"
        return httpx.Response(
            200,
            json={"result": {"UpdateResults": [{"Id": 710382063}]}},
        )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: _client_with_handler(
        settings, handler
    )
    payload = {
        "goal_id": 516513575,
        "value_rub": 700.0,
        "dry_run": False,
        "approved": True,
        "idempotency_key": "priority-goal-replay-001",
    }

    first = client.post("/yandex/campaigns/710382063/priority-goals", json=payload)
    second = client.post("/yandex/campaigns/710382063/priority-goals", json=payload)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    assert calls == ["get", "update", "get"]
