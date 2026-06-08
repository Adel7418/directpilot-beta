from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.yandex_direct import YandexDirectClient


def _settings(mode: str = "live_readonly") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token="SECRET-TOKEN")


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "approved": True,
        "idempotency_key": "vcard-kazan-001",
        "dry_run": True,
        "campaign_id": 710382063,
        "company_name": "РемБыт Казань",
        "city": "Казань",
        "phone": {
            "country_code": "7",
            "city_code": "843",
            "phone_number": "2916429",
        },
        "work_time": "0#6#07#00#24#00",
    }
    payload.update(overrides)
    return payload


def test_vcards_get_sends_required_field_names():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"VCards": []}})

    client = YandexDirectClient(settings=_settings(), transport=httpx.MockTransport(handler))
    result = client.vcards_get()

    assert result["ok"] is True
    assert captured["url"] == "https://api.direct.yandex.com/json/v5/vcards"
    assert captured["body"]["method"] == "get"
    assert "FieldNames" in captured["body"]["params"]
    assert "WorkTime" in captured["body"]["params"]["FieldNames"]
    assert "Phone" in captured["body"]["params"]["FieldNames"]
    assert "SECRET-TOKEN" not in str(result)


def test_vcards_add_posts_vcards_add_payload_without_leaking_token():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 12345}]}})

    client = YandexDirectClient(settings=_settings("live_write"), transport=httpx.MockTransport(handler))
    result = client.vcards_add(
        {
            "Country": "Россия",
            "City": "Казань",
            "CompanyName": "РемБыт Казань",
            "WorkTime": "0#6#07#00#24#00",
            "Phone": {"CountryCode": "7", "CityCode": "843", "PhoneNumber": "2916429"},
        }
    )

    assert result["ok"] is True
    assert captured["url"] == "https://api.direct.yandex.com/json/v5/vcards"
    assert captured["body"]["method"] == "add"
    vcard = captured["body"]["params"]["VCards"][0]
    assert vcard["Phone"] == {"CountryCode": "7", "CityCode": "843", "PhoneNumber": "2916429"}
    assert vcard["WorkTime"] == "0#6#07#00#24#00"
    assert "SECRET-TOKEN" not in str(result)


def test_vcard_endpoint_dry_run_never_calls_yandex_and_returns_audit_id():
    from app import main as main_mod

    settings = _settings("live_readonly")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called on vCard dry_run")

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj
    try:
        response = TestClient(app).post("/yandex/vcards", json=_payload())
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["source"] == "yandex"
    assert body["work_time"] == "0#6#07#00#24#00"
    assert body["audit_id"].startswith("audit_")
    assert "SECRET-TOKEN" not in response.text


def test_vcard_endpoint_live_readonly_rejects_real_write_before_network():
    from app import main as main_mod

    settings = _settings("live_readonly")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called in live_readonly vCard apply")

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj
    try:
        response = TestClient(app).post(
            "/yandex/vcards",
            json=_payload(idempotency_key="vcard-readonly-001", dry_run=False),
        )
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 502
    assert "live_readonly" in response.text
    assert "SECRET-TOKEN" not in response.text


def test_vcard_endpoint_live_write_calls_yandex_once_and_returns_vcard_id():
    from app import main as main_mod

    settings = _settings("live_write")
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 98765}]}})

    client_obj = YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))
    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    app.dependency_overrides[main_mod.get_yandex_client] = lambda: client_obj
    try:
        response = TestClient(app).post(
            "/yandex/vcards",
            json=_payload(idempotency_key="vcard-live-001", dry_run=False),
        )
    finally:
        app.dependency_overrides.pop(main_mod.get_settings, None)
        app.dependency_overrides.pop(main_mod.get_yandex_client, None)

    assert response.status_code == 200, response.text
    assert len(calls) == 1
    body = response.json()
    assert body["dry_run"] is False
    assert body["applied"] is True
    assert body["vcard_id"] == "98765"
    assert calls[0]["method"] == "add"
    assert calls[0]["params"]["VCards"][0]["City"] == "Казань"
    assert calls[0]["params"]["VCards"][0]["CampaignId"] == 710382063
    assert "SECRET-TOKEN" not in response.text
