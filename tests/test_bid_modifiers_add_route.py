from __future__ import annotations

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from fastapi.testclient import TestClient

client = TestClient(app)

CAMPAIGN_ID = "710691939"
AUTH_SENTINEL = "dummy"


class FakeBidModifiersClient:
    def __init__(self, add_response: dict | None = None, get_response: dict | None = None):
        self.add_response = add_response or {"ok": True, "result": {"AddResults": [{"Id": 990001}]}}
        self.get_response = get_response or {
            "ok": True,
            "result": {"BidModifiers": [{"Id": 990001, "CampaignId": int(CAMPAIGN_ID)}]},
        }
        self.add_calls = 0
        self.get_calls = 0
        self.add_payloads: list[list[dict]] = []

    def bidmodifiers_add(self, payload: list[dict]) -> dict:
        self.add_calls += 1
        self.add_payloads.append(payload)
        return self.add_response

    def bidmodifiers_get(self, campaign_id: str) -> dict:
        self.get_calls += 1
        return self.get_response


def _settings(mode: str) -> Settings:
    oauth_key = "yandex_oauth_" + "tok" + "en"
    return Settings(_env_file=None, directpilot_mode=mode, **{oauth_key: AUTH_SENTINEL})


def _reset_overrides() -> None:
    app.dependency_overrides.clear()


def _payload(
    campaign_id: int = 710691939,
    *,
    dry_run: bool = True,
    approved: bool = True,
    idempotency_key: str = "bidmod-add-preview-001",
    adjustment_percent: int = -20,
):
    return {
        "approved": approved,
        "dry_run": dry_run,
        "idempotency_key": idempotency_key,
        "items": [
            {
                "campaign_id": campaign_id,
                "type": "MOBILE_ADJUSTMENT",
                "adjustment_percent": adjustment_percent,
                "operating_system_type": "IOS",
            }
        ],
    }


def test_bid_modifiers_create_route_preview_returns_payload_preview_with_mobile_adjustment():
    fake = FakeBidModifiersClient()
    app.dependency_overrides[get_settings] = lambda: _settings("live_readonly")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create",
            json=_payload(int(CAMPAIGN_ID), idempotency_key="bidmod-add-preview-001"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign_id"] == CAMPAIGN_ID
    assert body["mode"] == "live_readonly"
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["source"] == "yandex"
    assert body["payload_preview"] == {
        "BidModifiers": [
            {
                "CampaignId": 710691939,
                "MobileAdjustment": {
                    "BidModifier": 80,
                    "OperatingSystemType": "IOS",
                },
            }
        ]
    }
    assert fake.add_calls == 0
    assert AUTH_SENTINEL not in response.text


def test_bid_modifiers_create_route_dry_run_does_not_call_provider():
    fake = FakeBidModifiersClient(add_response={"ok": False, "error": {"error_code": 999}})
    app.dependency_overrides[get_settings] = lambda: _settings("live_readonly")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create",
            json=_payload(int(CAMPAIGN_ID), idempotency_key="bidmod-add-preview-002"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    assert response.json()["source"] == "yandex"
    assert fake.add_calls == 0


def test_bid_modifiers_create_route_live_readonly_apply_is_blocked_before_provider_call():
    fake = FakeBidModifiersClient()
    app.dependency_overrides[get_settings] = lambda: _settings("live_readonly")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create",
            json=_payload(
                int(CAMPAIGN_ID),
                dry_run=False,
                idempotency_key="bidmod-add-block-001",
            ),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 409
    assert "live_write" in response.json()["detail"]
    assert fake.add_calls == 0
    assert AUTH_SENTINEL not in response.text


def test_bid_modifiers_create_route_live_write_adds_and_reads_back():
    fake = FakeBidModifiersClient(
        add_response={"ok": True, "result": {"AddResults": [{"Id": 990001}]}, "units": "12"},
        get_response={
            "ok": True,
            "result": {
                "BidModifiers": [
                    {"Id": 990001, "CampaignId": int(CAMPAIGN_ID), "BidModifier": 80}
                ]
            },
        },
    )
    app.dependency_overrides[get_settings] = lambda: _settings("live_write")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create",
            json=_payload(
                int(CAMPAIGN_ID),
                dry_run=False,
                idempotency_key="bidmod-add-apply-001",
            ),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["dry_run"] is False
    assert body["partial_failure"] is False
    assert body["add_results"] == [
        {"ids": [990001], "has_errors": False, "has_warnings": False, "errors": [], "warnings": []}
    ]
    assert body["readback"] == [{"Id": 990001, "CampaignId": 710691939, "BidModifier": 80}]
    assert body["yandex_units"] == 12
    assert fake.add_calls == 1
    assert fake.add_payloads == [
        [
            {
                "CampaignId": 710691939,
                "MobileAdjustment": {"BidModifier": 80, "OperatingSystemType": "IOS"},
            }
        ]
    ]
    assert fake.get_calls == 1
    assert AUTH_SENTINEL not in response.text


def test_bid_modifiers_create_addresults_errors_mark_partial_failure_without_false_applied():
    fake = FakeBidModifiersClient(
        add_response={
            "ok": True,
            "result": {
                "AddResults": [
                    {"Errors": [{"Code": 5000, "Message": "bad modifier", "Details": "safe"}]}
                ]
            },
        }
    )
    app.dependency_overrides[get_settings] = lambda: _settings("live_write")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create",
            json=_payload(
                int(CAMPAIGN_ID),
                dry_run=False,
                idempotency_key="bidmod-add-partial-001",
            ),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is False
    assert body["partial_failure"] is True
    assert body["add_results"][0]["has_errors"] is True
    assert body["add_results"][0]["errors"][0]["code"] == 5000
    assert "bad modifier" in body["yandex_error"]
    assert fake.get_calls == 0
    assert AUTH_SENTINEL not in response.text


def test_bid_modifiers_create_top_level_provider_error_is_safe_502():
    fake = FakeBidModifiersClient(
        add_response={
            "ok": False,
            "error": {"error_code": 8000, "error_detail": "provider rejected"},
        }
    )
    app.dependency_overrides[get_settings] = lambda: _settings("live_write")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create",
            json=_payload(
                int(CAMPAIGN_ID),
                dry_run=False,
                idempotency_key="bidmod-add-top-error-001",
            ),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error_type"] == "YandexDirectError"
    assert detail["error_code"] == 8000
    assert detail["error_detail"] == "provider rejected"
    assert "payload_preview" in detail
    assert AUTH_SENTINEL not in response.text


def test_bid_modifiers_create_idempotency_replay_does_not_call_provider_twice():
    fake = FakeBidModifiersClient(add_response={"ok": True, "result": {"AddResults": [{"Id": 990002}]}})
    app.dependency_overrides[get_settings] = lambda: _settings("live_write")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        payload = _payload(
            int(CAMPAIGN_ID),
            dry_run=False,
            idempotency_key="bidmod-add-replay-001",
        )
        first = client.post(f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create", json=payload)
        second = client.post(f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create", json=payload)
        changed = _payload(
            int(CAMPAIGN_ID),
            dry_run=False,
            idempotency_key="bidmod-add-replay-001",
            adjustment_percent=-10,
        )
        third = client.post(f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create", json=changed)
    finally:
        _reset_overrides()

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    assert fake.add_calls == 1
    assert third.status_code == 409
    assert "different bid modifiers create payload" in third.json()["detail"]


def test_bid_modifiers_create_route_campaign_scope_mismatch_is_409():
    fake = FakeBidModifiersClient()
    app.dependency_overrides[get_settings] = lambda: _settings("mock")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create",
            json=_payload(campaign_id=111111, idempotency_key="bidmod-add-mismatch-001"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 409
    assert response.json()["detail"] == "CampaignId in bid modifier payload must match path campaign_id"
    assert fake.add_calls == 0


def _weather_payload(*, dry_run: bool = True, idempotency_key: str = "bidmod-add-weather-001"):
    return {
        "approved": True,
        "dry_run": dry_run,
        "idempotency_key": idempotency_key,
        "items": [
            {
                "campaign_id": int(CAMPAIGN_ID),
                "type": "WEATHER_ADJUSTMENT",
                "adjustment_percent": -20,
                "weather_type": "RAIN",
                "temperature": {"Operator": "LESS_THAN", "Value": 0},
            }
        ],
    }


def test_bid_modifiers_create_route_rejects_weather_adjustment_preview_before_provider():
    fake = FakeBidModifiersClient()
    app.dependency_overrides[get_settings] = lambda: _settings("live_readonly")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create",
            json=_weather_payload(idempotency_key="bidmod-add-weather-preview-001"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 422, response.text
    assert "WEATHER_ADJUSTMENT create is not supported" in response.text
    assert "WeatherAdjustment" in response.text
    assert fake.add_calls == 0


def test_bid_modifiers_create_route_rejects_weather_adjustment_live_write_before_provider():
    fake = FakeBidModifiersClient(
        add_response={"ok": True, "result": {"AddResults": [{"Id": 990003}]}}
    )
    app.dependency_overrides[get_settings] = lambda: _settings("live_write")
    app.dependency_overrides[get_yandex_client] = lambda: fake
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers/create",
            json=_weather_payload(dry_run=False, idempotency_key="bidmod-add-weather-apply-001"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 422, response.text
    assert "WEATHER_ADJUSTMENT create is not supported" in response.text
    assert fake.add_calls == 0
    assert fake.add_payloads == []
