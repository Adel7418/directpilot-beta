from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.yandex_direct import YandexDirectClient

client = TestClient(app)

SECRET_TOKEN = "TOPSECRET-BIDMOD-001"
CAMPAIGN_ID = "710691939"


def _settings(mode: str) -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=SECRET_TOKEN)


def _reset_overrides() -> None:
    app.dependency_overrides.clear()


def _request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "approved": True,
        "idempotency_key": "bidmod-001",
        "dry_run": True,
        "adjustments": [{"age_range": "AGE_0_17", "adjustment_percent": -100}],
    }
    body.update(overrides)
    return body


def test_bid_modifiers_dry_run_previews_under_18_minus_100_without_write():
    app.dependency_overrides[get_settings] = lambda: _settings("live_readonly")
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers",
            json=_request(idempotency_key="bidmod-dry-001"),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is False
    assert body["dry_run"] is True
    assert body["source"] == "yandex"
    assert body["payload_preview"] == {
        "BidModifiers": [
            {
                "CampaignId": 710691939,
                "AgeRange": "AGE_0_17",
                "AdjustmentPercent": -100,
                "BidModifier": 0,
            }
        ]
    }
    assert SECRET_TOKEN not in response.text


def test_bid_modifiers_live_readonly_blocks_apply():
    app.dependency_overrides[get_settings] = lambda: _settings("live_readonly")
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers",
            json=_request(
                idempotency_key="bidmod-block-001",
                dry_run=False,
                adjustments=[{"modifier_id": 987654, "adjustment_percent": -100}],
            ),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 409
    assert "live_write" in response.json()["detail"]
    assert SECRET_TOKEN not in response.text


def test_bid_modifiers_apply_requires_existing_modifier_id():
    app.dependency_overrides[get_settings] = lambda: _settings("live_write")
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers",
            json=_request(idempotency_key="bidmod-missing-id-001", dry_run=False),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 409
    assert "modifier_id is required" in response.json()["detail"]
    assert SECRET_TOKEN not in response.text


def test_bid_modifiers_mocked_live_write_apply_calls_set_and_readback_once():
    settings = _settings("live_write")
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured.append({"url": str(request.url), "body": body})
        if body["method"] == "set":
            return httpx.Response(
                200,
                headers={"Units": "3"},
                json={"result": {"SetResults": [{"Id": 987654}]}},
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "BidModifiers": [
                        {"Id": 987654, "CampaignId": 710691939, "BidModifier": 0}
                    ]
                }
            },
        )

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: client_obj
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers",
            json=_request(
                idempotency_key="bidmod-apply-001",
                dry_run=False,
                adjustments=[{"modifier_id": 987654, "adjustment_percent": -100}],
            ),
        )
        replay = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers",
            json=_request(
                idempotency_key="bidmod-apply-001",
                dry_run=False,
                adjustments=[{"modifier_id": 987654, "adjustment_percent": -100}],
            ),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    assert replay.status_code == 200, replay.text
    body = response.json()
    assert body["applied"] is True
    assert body["yandex_units"] == 3
    assert body["readback"] == [
        {"Id": 987654, "CampaignId": 710691939, "BidModifier": 0}
    ]
    # First request performs set + readback; idempotency replay performs no provider call.
    assert len(captured) == 2
    assert captured[0]["url"] == "https://api.direct.yandex.com/json/v5/bidmodifiers"
    assert captured[0]["body"] == {
        "method": "set",
        "params": {"BidModifiers": [{"Id": 987654, "BidModifier": 0}]},
    }
    assert captured[1]["body"]["method"] == "get"
    assert SECRET_TOKEN not in response.text


def test_bid_modifiers_setresults_errors_mark_partial_failure_without_false_applied():
    settings = _settings("live_write")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["method"] == "set":
            return httpx.Response(
                200,
                headers={"Units": "4"},
                json={
                    "result": {
                        "SetResults": [
                            {
                                "Id": 987654,
                                "Errors": [
                                    {
                                        "Code": 9300,
                                        "Message": "Invalid bid modifier",
                                        "Details": "redacted detail",
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "BidModifiers": [
                        {"Id": 987654, "CampaignId": 710691939, "BidModifier": 100}
                    ]
                }
            },
        )

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: client_obj
    try:
        response = client.post(
            f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers",
            json=_request(
                idempotency_key="bidmod-partial-001",
                dry_run=False,
                adjustments=[{"modifier_id": 987654, "adjustment_percent": -100}],
            ),
        )
    finally:
        _reset_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is False
    assert body["partial_failure"] is True
    assert body["set_results"] == [
        {
            "modifier_id": 987654,
            "has_errors": True,
            "has_warnings": False,
            "errors": [
                {
                    "code": 9300,
                    "message": "Invalid bid modifier",
                    "details": "redacted detail",
                }
            ],
            "warnings": [],
        }
    ]
    assert "modifier_id=987654" in body["yandex_error"]
    assert SECRET_TOKEN not in response.text


def test_bid_modifiers_unexpected_store_error_returns_502_without_raw_exception():
    app.dependency_overrides[get_settings] = lambda: _settings("live_write")

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("database exploded TOPSECRET-BIDMOD-001")

    try:
        from app import main as main_mod

        original = main_mod.store.yandex_bid_modifiers_update
        setattr(main_mod.store, "yandex_bid_modifiers_update", boom)
        try:
            response = client.post(
                f"/yandex/campaigns/{CAMPAIGN_ID}/bid-modifiers",
                json=_request(
                    idempotency_key="bidmod-unexpected-001",
                    dry_run=False,
                    adjustments=[{"modifier_id": 987654, "adjustment_percent": -100}],
                ),
            )
        finally:
            setattr(main_mod.store, "yandex_bid_modifiers_update", original)
    finally:
        _reset_overrides()

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["detail"]["error_type"] == "YandexDirectError"
    assert body["detail"]["message"] == "unexpected error during bid modifiers update: RuntimeError"
    assert "database exploded" not in response.text
    assert SECRET_TOKEN not in response.text
