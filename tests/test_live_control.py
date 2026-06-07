"""Tests for live control: limited pause/resume writes against Yandex Direct.

These tests cover the live-control adapter and the store / endpoint flow when
directpilot_mode is sandbox or live_write. live_readonly remains dry-run only.
They must:

- never make a real network call (httpx.MockTransport is used)
- never leak the OAuth token in responses, audit details, or error messages
- respect dry_run, approved, and idempotency_key
- surface YandexDirectError / HTTP errors as HTTP 502
- record audit events with action, dry_run, source, reason, yandex_result/error
- avoid duplicate live calls for the same idempotency_key
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.models import YandexControlRequest
from app.store import store
from app.yandex_direct import YandexDirectClient, YandexDirectError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_client(
    settings: Settings, handler
) -> YandexDirectClient:
    return YandexDirectClient(
        settings=settings, transport=httpx.MockTransport(handler)
    )


# ---------------------------------------------------------------------------
# YandexDirectClient adapter: suspend / resume
# ---------------------------------------------------------------------------


def test_suspend_campaign_posts_to_campaigns_service_with_method_suspend():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": [{"Id": 42}]})

    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="t-secret"
    )
    client = _make_client(settings, handler)

    result = client.suspend_campaign(42)

    assert result["ok"] is True
    assert captured["url"].endswith("/campaigns")
    assert captured["body"]["method"] == "suspend"
    assert captured["body"]["params"] == {"CampaignIds": [42]}


def test_resume_campaign_posts_method_resume_with_campaign_ids():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": [{"Id": 7}]})

    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="t-secret"
    )
    client = _make_client(settings, handler)

    result = client.resume_campaign("cmp-7")

    assert result["ok"] is True
    assert captured["url"].endswith("/campaigns")
    assert captured["body"]["method"] == "resume"
    assert captured["body"]["params"] == {"CampaignIds": ["cmp-7"]}


def test_suspend_campaign_uses_live_base_url_in_live_write_mode():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"result": [{"Id": 1}]})

    settings = Settings(
        _env_file=None,
        directpilot_mode="live_write",
        yandex_oauth_token="t",
    )
    client = _make_client(settings, handler)

    client.suspend_campaign(1)

    assert captured["url"].startswith("https://api.direct.yandex.com/json/v5/")


def test_suspend_campaign_surfaces_yandex_error_and_does_not_leak_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 58, "error_detail": "Незавершенная регистрация"}},
        )

    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="super-secret"
    )
    client = _make_client(settings, handler)

    result = client.suspend_campaign(99)

    assert result["ok"] is False
    assert result["error"]["error_code"] == 58
    assert "super-secret" not in str(result)


def test_suspend_campaign_missing_token_raises_before_network_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be reached when token is missing")

    settings = Settings(_env_file=None, directpilot_mode="sandbox")
    client = _make_client(settings, handler)

    with pytest.raises(YandexDirectError):
        client.suspend_campaign(1)


def test_suspend_campaign_normalizes_numeric_string_campaign_id_to_int():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": [{"Id": 123456}]})

    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="t"
    )
    client = _make_client(settings, handler)

    result = client.suspend_campaign("123456")

    assert result["ok"] is True
    # String id "123456" must be coerced to a numeric int in the
    # outgoing payload, since Direct v5 expects numeric CampaignIds.
    assert captured["body"]["params"] == {"CampaignIds": [123456]}
    assert isinstance(captured["body"]["params"]["CampaignIds"][0], int)


def test_non_dict_error_envelope_returns_safe_fallback_without_leaking_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": "upstream-gateway-boom-with-secret-xyz"},
        )

    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="TOPSECRET-TOKEN"
    )
    client = _make_client(settings, handler)

    result = client.clients_get()

    assert result["ok"] is False
    assert result["error"]["error_code"] is None
    assert result["error"]["message"] == "Yandex Direct returned a non-structured error"
    # The raw error string is exposed via error_detail for diagnostics,
    # but the OAuth token must never appear in the response envelope.
    assert "TOPSECRET-TOKEN" not in str(result)
    assert "secret-xyz" in result["error"]["error_detail"]


# ---------------------------------------------------------------------------
# store: live control flow
# ---------------------------------------------------------------------------


def _fresh_settings(mode: str, token: str | None = "t") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=token)


def test_store_live_control_dry_run_does_not_call_yandex_and_reports_source_yandex():
    settings = _fresh_settings("sandbox")
    client = _make_client(
        settings,
        lambda req: (_ for _ in ()).throw(
            AssertionError("network must not be called on dry_run")
        ),
    )

    result = store.yandex_control(
        "cmp-live-1",
        "pause",
        YandexControlRequest(
            approved=True,
            idempotency_key="live-dry-001",
            dry_run=True,
            reason="review",
        ),
        settings=settings,
        client=client,
    )

    assert result.source == "yandex"
    assert result.dry_run is True
    assert result.applied is False
    assert result.new_status == "paused"


def test_store_live_control_dry_run_false_calls_yandex_and_marks_applied():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.setdefault("calls", 0)
        captured["calls"] += 1
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": [{"Id": 12}]})

    settings = _fresh_settings("sandbox")
    client = _make_client(settings, handler)

    result = store.yandex_control(
        "cmp-live-2",
        "resume",
        YandexControlRequest(
            approved=True,
            idempotency_key="live-apply-001",
            dry_run=False,
            reason="daily cap",
        ),
        settings=settings,
        client=client,
    )

    assert result.source == "yandex"
    assert result.dry_run is False
    assert result.applied is True
    assert result.new_status == "active"
    assert captured["calls"] == 1
    assert captured["body"]["method"] == "resume"
    assert captured["body"]["params"] == {"CampaignIds": ["cmp-live-2"]}


def test_store_live_control_idempotency_avoids_duplicate_live_calls():
    captured: dict[str, Any] = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        return httpx.Response(200, json={"result": [{"Id": 1}]})

    settings = _fresh_settings("sandbox")
    client = _make_client(settings, handler)
    payload = YandexControlRequest(
        approved=True,
        idempotency_key="live-idem-1",
        dry_run=False,
    )

    r1 = store.yandex_control(
        "cmp-live-3", "pause", payload, settings=settings, client=client
    )
    r2 = store.yandex_control(
        "cmp-live-3", "pause", payload, settings=settings, client=client
    )

    assert r1.audit_id == r2.audit_id
    assert captured["calls"] == 1


def test_store_live_control_yandex_error_raises_yandex_direct_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 88, "error_detail": "Bad campaign id"}},
        )

    settings = _fresh_settings("sandbox")
    client = _make_client(settings, handler)

    with pytest.raises(YandexDirectError):
        store.yandex_control(
            "cmp-bad",
            "pause",
            YandexControlRequest(
                approved=True,
                idempotency_key="live-err-1",
                dry_run=False,
            ),
            settings=settings,
            client=client,
        )


def test_store_live_control_http_error_raises_yandex_direct_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream gone")

    settings = _fresh_settings("sandbox")
    client = _make_client(settings, handler)

    with pytest.raises(YandexDirectError):
        store.yandex_control(
            "cmp-bad",
            "pause",
            YandexControlRequest(
                approved=True,
                idempotency_key="live-http-1",
                dry_run=False,
            ),
            settings=settings,
            client=client,
        )


def test_store_live_readonly_rejects_dry_run_false_before_network_call():
    settings = _fresh_settings("live_readonly", token="TOPSECRET")
    client = _make_client(
        settings,
        lambda req: (_ for _ in ()).throw(
            AssertionError("network must not be called in live_readonly apply")
        ),
    )

    with pytest.raises(YandexDirectError) as exc:
        store.yandex_control(
            "cmp-readonly",
            "pause",
            YandexControlRequest(
                approved=True,
                idempotency_key="readonly-block-1",
                dry_run=False,
            ),
            settings=settings,
            client=client,
        )

    assert "live_readonly" in str(exc.value)
    assert "TOPSECRET" not in str(exc.value)


def test_store_live_control_audit_event_records_yandex_result_and_no_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [{"Id": 21}]})

    settings = _fresh_settings("sandbox", token="TOPSECRET-TOKEN")
    client = _make_client(settings, handler)

    before = len(store.audit_events)
    store.yandex_control(
        "cmp-aud",
        "pause",
        YandexControlRequest(
            approved=True,
            idempotency_key="live-aud-1",
            dry_run=False,
            reason="stop runaway spend",
        ),
        settings=settings,
        client=client,
    )

    new_events = store.audit_events[before:]
    assert new_events, "expected at least one audit event"
    ev = new_events[-1]
    assert ev.action == "yandex_pause_requested"
    assert ev.dry_run is False
    # details should include source=yandex and reason, but NEVER the token
    assert ev.details is not None
    assert ev.details.get("source") == "yandex"
    assert ev.details.get("reason") == "stop runaway spend"
    assert "TOPSECRET" not in json.dumps(ev.details)
    # yandex_result captured (best-effort)
    assert ev.details.get("yandex_result") is not None


def test_store_live_control_audit_event_records_yandex_error_on_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"error": {"error_code": 88, "error_detail": "no campaign"}}
        )

    settings = _fresh_settings("sandbox", token="TOPSECRET-TOKEN")
    client = _make_client(settings, handler)

    before = len(store.audit_events)
    with pytest.raises(YandexDirectError):
        store.yandex_control(
            "cmp-aud-err",
            "pause",
            YandexControlRequest(
                approved=True,
                idempotency_key="live-aud-err",
                dry_run=False,
                reason="err",
            ),
            settings=settings,
            client=client,
        )

    new_events = store.audit_events[before:]
    assert new_events, "expected audit event for the failed attempt"
    ev = new_events[-1]
    assert ev.action == "yandex_pause_failed"
    assert ev.dry_run is False
    assert ev.details is not None
    assert ev.details.get("source") == "yandex"
    assert ev.details.get("yandex_error") is not None
    assert "TOPSECRET" not in json.dumps(ev.details)


def test_store_mock_mode_keeps_source_mock_for_control():
    settings = _fresh_settings("mock", token=None)
    # No client needed in mock; pass None to be explicit
    result = store.yandex_control(
        "cmp-mock",
        "pause",
        YandexControlRequest(
            approved=True,
            idempotency_key="mock-001",
            dry_run=False,
        ),
        settings=settings,
        client=None,
    )
    assert result.source == "mock"
    assert result.applied is True
    assert result.new_status == "paused"


# ---------------------------------------------------------------------------
# Endpoints: pause / resume in live modes
# ---------------------------------------------------------------------------


def _override_settings_and_client(client: YandexDirectClient | None):
    """Override get_settings and the yandex client factory in FastAPI app."""
    from app import main as main_mod

    def _settings_override() -> Settings:
        return main_mod._test_settings  # type: ignore[attr-defined]

    def _client_factory(settings: Settings) -> YandexDirectClient | None:
        return client

    app.dependency_overrides[main_mod.get_settings] = _settings_override
    app.dependency_overrides[main_mod.get_yandex_client] = _client_factory
    return lambda: (
        app.dependency_overrides.pop(main_mod.get_settings, None),
        app.dependency_overrides.pop(main_mod.get_yandex_client, None),
    )


def test_pause_endpoint_sandbox_dry_run_returns_200_source_yandex_applied_false():
    from app import main as main_mod

    settings = _fresh_settings("sandbox", token="t")
    main_mod._test_settings = settings  # type: ignore[attr-defined]

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called on dry_run")

    client_obj = _make_client(settings, handler)
    cleanup = _override_settings_and_client(client_obj)
    try:
        test_client = TestClient(app)
        response = test_client.post(
            "/yandex/campaigns/cmp-sandbox-1/pause",
            json={
                "approved": True,
                "idempotency_key": "ep-dry-1",
                "dry_run": True,
                "reason": "review",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "yandex"
        assert body["dry_run"] is True
        assert body["applied"] is False
        assert body["new_status"] == "paused"
    finally:
        cleanup()


def test_pause_endpoint_sandbox_apply_calls_yandex_and_returns_200():
    from app import main as main_mod

    settings = _fresh_settings("sandbox", token="t")
    main_mod._test_settings = settings  # type: ignore[attr-defined]

    captured: dict[str, Any] = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        return httpx.Response(200, json={"result": [{"Id": 1}]})

    client_obj = _make_client(settings, handler)
    cleanup = _override_settings_and_client(client_obj)
    try:
        test_client = TestClient(app)
        response = test_client.post(
            "/yandex/campaigns/cmp-sandbox-2/pause",
            json={
                "approved": True,
                "idempotency_key": "ep-apply-1",
                "dry_run": False,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "yandex"
        assert body["applied"] is True
        assert captured["calls"] == 1
    finally:
        cleanup()


def test_pause_endpoint_sandbox_yandex_error_returns_502_without_token():
    from app import main as main_mod

    settings = _fresh_settings("sandbox", token="MUST-NOT-LEAK")
    main_mod._test_settings = settings  # type: ignore[attr-defined]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"error": {"error_code": 88, "error_detail": "nope"}}
        )

    client_obj = _make_client(settings, handler)
    cleanup = _override_settings_and_client(client_obj)
    try:
        test_client = TestClient(app)
        response = test_client.post(
            "/yandex/campaigns/cmp-sandbox-3/pause",
            json={
                "approved": True,
                "idempotency_key": "ep-err-1",
                "dry_run": False,
            },
        )
        assert response.status_code == 502
        body_text = response.text
        assert "MUST-NOT-LEAK" not in body_text
        body = response.json()
        assert body["detail"]["error_type"] == "YandexDirectError"
    finally:
        cleanup()


def test_pause_endpoint_live_readonly_dry_run_false_returns_409_without_network():
    from app import main as main_mod

    settings = _fresh_settings("live_readonly", token="READONLY-NO-LEAK")
    main_mod._test_settings = settings  # type: ignore[attr-defined]

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called in live_readonly apply")

    client_obj = _make_client(settings, handler)
    cleanup = _override_settings_and_client(client_obj)
    try:
        test_client = TestClient(app)
        response = test_client.post(
            "/yandex/campaigns/cmp-readonly/pause",
            json={
                "approved": True,
                "idempotency_key": "ep-readonly-1",
                "dry_run": False,
            },
        )
        assert response.status_code == 409
        assert "READONLY-NO-LEAK" not in response.text
        assert "live_write" in response.text
    finally:
        cleanup()


def test_pause_endpoint_sandbox_idempotency_avoids_duplicate_live_calls():
    from app import main as main_mod

    settings = _fresh_settings("sandbox", token="t")
    main_mod._test_settings = settings  # type: ignore[attr-defined]

    captured: dict[str, Any] = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        return httpx.Response(200, json={"result": [{"Id": 1}]})

    client_obj = _make_client(settings, handler)
    cleanup = _override_settings_and_client(client_obj)
    try:
        test_client = TestClient(app)
        body = {
            "approved": True,
            "idempotency_key": "ep-idem-1",
            "dry_run": False,
        }
        r1 = test_client.post("/yandex/campaigns/cmp-sandbox-4/pause", json=body)
        r2 = test_client.post("/yandex/campaigns/cmp-sandbox-4/pause", json=body)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["audit_id"] == r2.json()["audit_id"]
        assert captured["calls"] == 1
    finally:
        cleanup()


def test_resume_endpoint_sandbox_dry_run_returns_source_yandex():
    from app import main as main_mod

    settings = _fresh_settings("sandbox", token="t")
    main_mod._test_settings = settings  # type: ignore[attr-defined]

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called on dry_run")

    client_obj = _make_client(settings, handler)
    cleanup = _override_settings_and_client(client_obj)
    try:
        test_client = TestClient(app)
        response = test_client.post(
            "/yandex/campaigns/cmp-sandbox-r/resume",
            json={
                "approved": True,
                "idempotency_key": "ep-resume-1",
                "dry_run": True,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "resume"
        assert body["source"] == "yandex"
        assert body["applied"] is False
        assert body["new_status"] == "active"
    finally:
        cleanup()


def test_live_control_audit_event_in_endpoint_response_has_no_token():
    from app import main as main_mod

    settings = _fresh_settings("sandbox", token="AUDIT-NO-LEAK")
    main_mod._test_settings = settings  # type: ignore[attr-defined]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [{"Id": 1}]})

    client_obj = _make_client(settings, handler)
    cleanup = _override_settings_and_client(client_obj)
    try:
        test_client = TestClient(app)
        test_client.post(
            "/yandex/campaigns/cmp-audit-endpoint/pause",
            json={
                "approved": True,
                "idempotency_key": "ep-audit-1",
                "dry_run": False,
                "reason": "daily cap reached",
            },
        )
        log = test_client.get("/audit-log").json()["items"]
        recent = [
            e
            for e in log
            if e["action"] == "yandex_pause_requested"
            and e["entity"] == "cmp-audit-endpoint"
        ]
        assert recent, "expected an audit event for the live control"
        ev = recent[-1]
        assert ev["dry_run"] is False
        assert "AUDIT-NO-LEAK" not in json.dumps(ev.get("details", {}))
    finally:
        cleanup()
