"""Tests for the staged live-create campaign endpoint.

This endpoint is the most safety-sensitive write in DirectPilot: a
single mis-call here can create a billable campaign on the user's
production Yandex account. The contract is identical to the rest of
the product:

* ``dry_run=True`` is ALWAYS allowed and NEVER performs a network
  write. The response is the exact v5 ``campaigns.add`` payload that
  WOULD be sent, with ``applied=False``.
* In ``live_readonly`` mode, ``dry_run=False`` is REJECTED before
  any network call (HTTP 409). The response must mention
  ``live_readonly`` and must NOT echo the OAUTH token.
* In ``live_write`` mode, ``dry_run=False`` is allowed only when
  ``approved=True`` and an ``idempotency_key`` is supplied. Without
  either, the request is rejected.
* Audit events are recorded for both dry-run and live apply. They
  never contain the OAUTH token.
* The follow-up stages (``adgroups.add``, ``ads.add``,
  ``keywords.add``, ``negativekeywordsharedsets.add``) are NOT
  performed in stage 1; the response carries a stable
  ``not_implemented`` list.

The tests below pin this contract. We do NOT exercise any real
Yandex ``campaigns.add`` call — the live-write success path is
verified via httpx.MockTransport so it stays hermetic.
"""

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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(mode: str, token: str | None = "t-test-token") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=token)


def _client_with_handler(settings: Settings, handler) -> YandexDirectClient:
    """Build a YandexDirectClient whose transport is the given httpx handler.

    The handler is wrapped so we can assert that no real network call
    happened on the rejected / dry-run paths.
    """

    def _transport(request: httpx.Request) -> httpx.Response:
        try:
            return handler(request)
        except AssertionError:
            raise

    transport = httpx.MockTransport(_transport)
    return YandexDirectClient(settings=settings, transport=transport)


def _create_draft_payload() -> dict[str, Any]:
    """A minimal valid campaign-draft request that the live-create flow
    can read. The text-campaign family requires a non-empty name and a
    positive monthly budget."""
    return {
        "name": "DirectPilot live-create test",
        "business_type": "local_services",
        "region": "Казань",
        "monthly_budget": 30000.0,
        "landing_url": "https://example.com/landing",
    }


def _create_draft() -> str:
    """Create a campaign draft and return its id. Used by every test
    that exercises the live-create path."""
    response = client.post(
        "/campaign-drafts", json=_create_draft_payload()
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _live_create_payload(
    draft_id: str,
    *,
    approved: bool = True,
    dry_run: bool = True,
    idempotency_key: str = "live-create-001",
    start_date: str | None = "2026-06-15",
    counter_ids: list[int] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "draft_id": draft_id,
        "approved": approved,
        "idempotency_key": idempotency_key,
        "dry_run": dry_run,
    }
    if start_date is not None:
        body["start_date"] = start_date
    if counter_ids is not None:
        body["counter_ids"] = counter_ids
    return body


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def test_live_create_dry_run_in_live_readonly_does_not_call_yandex():
    """In live_readonly, dry_run=True is allowed and must NOT call
    Yandex. The endpoint must return the v5 payload preview, NOT a
    4xx error."""
    settings = _settings("live_readonly", token="TOPSECRET-LC-DR")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 1}]}})

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(draft_id),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["mode"] == "live_readonly"
    assert body["source"] == "yandex"
    # No network call on dry-run.
    assert network_called["calls"] == 0
    # Token never echoed.
    assert "TOPSECRET-LC-DR" not in response.text
    # The payload preview is the v5 ``campaigns.add`` shape.
    assert body["payload_preview"] is not None
    assert body["payload_preview"]["method"] == "campaigns.add"
    assert "Campaigns" in body["payload_preview"]["params"]
    assert len(body["payload_preview"]["params"]["Campaigns"]) == 1
    # Full chain is implemented; dry-run does not execute stages,
    # but only shared negative sets remain outside the chain.
    assert body["not_implemented"] == ["negativekeywordsharedsets.add"]
    assert body["stages_executed"] == []


def test_live_create_apply_in_sandbox_is_rejected_before_network_call():
    """In ``sandbox`` mode, real apply must be REJECTED before any
    network call. ``sandbox`` shares the v5 ``campaigns.add`` write
    shape with production — the only safe behaviour is to refuse the
    apply with a clear 409.

    ``dry_run=True`` is still allowed (sandbox is live-ish, not a
    write path) — this test pins the ``dry_run=False`` apply path.
    """
    settings = _settings("sandbox", token="TOPSECRET-LC-SANDBOX")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(
            200, json={"result": {"AddResults": [{"Id": 1}]}}
        )

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-sandbox-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "sandbox" in str(detail).lower()
    assert "TOPSECRET-LC-SANDBOX" not in response.text
    # The gate MUST be no-network: the v5 ``campaigns.add`` helper
    # was never called.
    assert network_called["calls"] == 0


def test_live_create_apply_in_mock_is_rejected_before_network_call():
    """In ``mock`` mode, real apply must be REJECTED before any
    network call. ``mock`` has no live client at all — silently
    returning ``applied=False`` (a dry-run shape) is a lie that
    would mask a misconfigured deploy.

    The endpoint must 409, mention ``mock`` in the reason, and
    perform zero network calls.
    """
    settings = _settings("mock", token="TOPSECRET-LC-MOCK")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(
            200, json={"result": {"AddResults": [{"Id": 1}]}}
        )

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-mock-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "mock" in str(detail).lower()
    assert "TOPSECRET-LC-MOCK" not in response.text
    assert network_called["calls"] == 0


def test_live_create_apply_in_live_readonly_is_blocked_before_network_call():
    """In live_readonly, real apply is REJECTED before any network call."""
    settings = _settings("live_readonly", token="TOPSECRET-LC-BLOCK")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 1}]}})

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-block-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "live_readonly" in str(detail)
    assert "TOPSECRET-LC-BLOCK" not in response.text
    assert network_called["calls"] == 0


def test_live_create_without_approved_is_rejected():
    """The endpoint must reject ``approved=False`` with HTTP 409."""
    settings = _settings("live_write", token="TOPSECRET-LC-NOAPP")
    draft_id = _create_draft()

    response = client.post(
        "/yandex/campaigns/live-create",
        json=_live_create_payload(
            draft_id,
            approved=False,
            dry_run=False,
            idempotency_key="live-create-noapp-001",
        ),
    )
    assert response.status_code == 409, response.text
    assert "approval" in response.text.lower()
    assert "TOPSECRET-LC-NOAPP" not in response.text


def test_live_create_missing_draft_returns_404():
    """A non-existent draft id must return HTTP 404, not 5xx."""
    settings = _settings("live_write", token="TOPSECRET-LC-404")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                "draft_does_not_exist",
                dry_run=True,
                idempotency_key="live-create-404-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404, response.text
    assert "TOPSECRET-LC-404" not in response.text


# ---------------------------------------------------------------------------
# payload shape (v5 contract)
# ---------------------------------------------------------------------------


def test_live_create_dry_run_payload_uses_confirmed_v5_text_campaign_shape():
    """The payload preview MUST match the v5 ``campaigns.add`` text-campaign
    contract. We never invent field names; we mirror the v5 docs literally.

    The v5 contract for a text campaign is:

    .. code-block:: json

        {
          "Name": "...",
          "StartDate": "YYYY-MM-DD",
          "TextCampaign": {
            "BiddingStrategy": {"Strategy": "AVERAGE_CPC"},
            "CounterIds": [<int>, ...]  // optional
          },
          "DailyBudget": {"Amount": <int micros>, "Currency": "RUB"}  // optional
        }
    """
    draft_id = _create_draft()
    # Set a daily budget on the draft so the payload includes
    # ``DailyBudget``. We do this via the PATCH endpoint so the
    # endpoint is exercised end-to-end.
    budget = client.patch(
        f"/campaign-drafts/{draft_id}/budget",
        json={"daily_budget": 1500.0, "strategy": "manual"},
    )
    assert budget.status_code == 200, budget.text

    response = client.post(
        "/yandex/campaigns/live-create",
        json=_live_create_payload(
            draft_id,
            counter_ids=[98765, 98766],
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    campaign = body["payload_preview"]["params"]["Campaigns"][0]
    # Mandatory fields
    assert campaign["Name"].startswith("DirectPilot live-create test")
    assert campaign["StartDate"] == "2026-06-15"
    assert "Status" not in campaign
    assert "TextCampaign" in campaign
    assert campaign["TextCampaign"]["BiddingStrategy"]["Strategy"] == "AVERAGE_CPC"
    assert campaign["TextCampaign"]["CounterIds"] == [98765, 98766]
    # DailyBudget is in micro-units (1/1_000_000 of currency).
    assert campaign["DailyBudget"] == {
        "Amount": int(1500.0 * 1_000_000),
        "Currency": "RUB",
    }


def test_live_create_dry_run_omits_start_date_when_none():
    """When ``start_date`` is None on the request, the v5 payload
    preview MUST NOT carry a ``StartDate: null`` key. v5 rejects
    ``null`` for required string fields; the v5 client must omit the
    key entirely so the v5 service can apply its own default (today
    UTC). The endpoint contract: ``start_date`` is optional, and the
    payload MUST reflect that.
    """
    draft_id = _create_draft()

    # Note: ``start_date`` is intentionally omitted from the request
    # body so the model default of ``None`` is used.
    body: dict[str, Any] = {
        "draft_id": draft_id,
        "approved": True,
        "idempotency_key": "live-create-nostartdate-001",
        "dry_run": True,
    }
    response = client.post("/yandex/campaigns/live-create", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()["payload_preview"]
    campaign = payload["params"]["Campaigns"][0]
    # StartDate key must not be present (not even with value None).
    assert "StartDate" not in campaign, campaign
    # Sanity: the rest of the v5 contract is still honoured.
    assert "Name" in campaign
    assert "Status" not in campaign
    assert "TextCampaign" in campaign


# ---------------------------------------------------------------------------
# live_write success path
# ---------------------------------------------------------------------------


def test_live_create_apply_in_live_write_calls_campaigns_add_and_returns_new_id():
    """``live_write`` + ``approved`` + ``idempotency_key`` + ``dry_run=False``
    must dispatch to :meth:`YandexDirectClient.campaigns_add` and
    return the new campaign id from the Yandex ``AddResults``
    envelope."""
    settings = _settings("live_write", token="TOPSECRET-LC-FULL")
    captured: dict[str, Any] = {"calls": 0, "urls": [], "bodies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        url = str(request.url)
        body = json.loads(request.content.decode())
        captured["urls"].append(url)
        captured["bodies"].append(body)
        return httpx.Response(
            200,
            json={"result": {"AddResults": [{"Id": 424242}]}},
        )

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-full-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is False
    assert body["applied"] is True
    assert body["mode"] == "live_write"
    assert body["source"] == "yandex"
    assert body["campaign_id"] == "424242"
    assert "TOPSECRET-LC-FULL" not in response.text
    # Exactly one HTTP call: campaigns.add. The follow-up stages are
    # NOT performed in stage 1.
    assert captured["calls"] == 1
    assert captured["urls"] == [
        "https://api.direct.yandex.com/json/v5/campaigns"
    ]
    body_sent = captured["bodies"][0]
    assert body_sent["method"] == "add"
    assert "Campaigns" in body_sent["params"]
    assert len(body_sent["params"]["Campaigns"]) == 1
    # Audit event recorded.
    applied_events = [
        event
        for event in store.audit_events
        if event.action == "live_create_campaign_requested"
    ]
    assert applied_events, "expected live_create_campaign_requested audit event"
    last = applied_events[-1]
    assert last.dry_run is False
    assert last.details is not None
    assert last.details.get("campaign_id") == "424242"
    assert "TOPSECRET-LC-FULL" not in json.dumps(last.details, ensure_ascii=False)
    # Empty draft structure skips stages 2..4; only shared negative sets remain outside the chain.
    assert body["stages_executed"] == ["campaigns.add"]
    assert body["not_implemented"] == ["negativekeywordsharedsets.add"]


def test_live_create_idempotency_key_avoids_duplicate_live_calls():
    """A replay with the same ``idempotency_key`` MUST NOT re-send to
    Yandex. The second response is the cached first response."""
    settings = _settings("live_write", token="TOPSECRET-LC-IDEM")
    captured: dict[str, Any] = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        return httpx.Response(
            200,
            json={"result": {"AddResults": [{"Id": 99}]}},
        )

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        body = _live_create_payload(
            draft_id,
            dry_run=False,
            idempotency_key="live-create-idem-001",
        )
        r1 = client.post("/yandex/campaigns/live-create", json=body)
        r2 = client.post("/yandex/campaigns/live-create", json=body)
    finally:
        app.dependency_overrides.clear()

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["audit_id"] == r2.json()["audit_id"]
    assert r1.json()["campaign_id"] == r2.json()["campaign_id"]
    # Only the first call hit the network.
    assert captured["calls"] == 1


# ---------------------------------------------------------------------------
# failure modes
# ---------------------------------------------------------------------------

def test_live_create_apply_records_failed_audit_on_yandex_error():
    """If Yandex rejects ``campaigns.add``, audit
    ``live_create_campaign_failed`` and surface as 502 with no token
    leakage.
    """
    settings = _settings("live_write", token="TOPSECRET-LC-FAIL")
    captured: dict[str, Any] = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        return httpx.Response(
            200,
            json={"error": {"error_code": 53, "error_detail": "Bad campaign params"}},
        )

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-fail-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502, response.text
    assert "TOPSECRET-LC-FAIL" not in response.text
    failed_events = [
        e
        for e in store.audit_events
        if e.action == "live_create_campaign_failed"
    ]
    assert failed_events, "expected live_create_campaign_failed audit event"
    last = failed_events[-1]
    assert "TOPSECRET-LC-FAIL" not in json.dumps(last.details, ensure_ascii=False)
    assert "campaigns.add" in (last.details or {}).get("yandex_error", "")


def test_live_create_apply_addresults_errors_treated_as_failed():
    """v5 ``AddResults`` items each carry ``Id``, ``Errors`` and
    ``Warnings``. The Direct API v5 contract says: when ``Errors``
    is non-empty OR ``Id`` is missing, that item failed — the
    ``ok=true`` envelope does NOT mean the campaign was created.

    A safe client MUST inspect every ``AddResults`` item, and if any
    item is failed, surface it as :class:`YandexDirectError` with a
    502, NOT as ``applied=True`` with ``campaign_id=None``.

    This is the most dangerous silent-failure mode in the live-create
    path: a v5 ``ok`` response with a per-item ``Errors`` array would
    otherwise be returned as ``applied=True`` with a None campaign
    id, lying to the operator.
    """
    settings = _settings("live_write", token="TOPSECRET-LC-ADDRES")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        # v5 contract: ok=true envelope, but the AddResults item
        # failed with a per-item Errors array and no Id.
        return httpx.Response(
            200,
            json={
                "result": {
                    "AddResults": [
                        {
                            "Errors": [
                                {
                                    "Code": 1520,
                                    "Message": "Invalid StartDate",
                                    "Details": "StartDate must be YYYY-MM-DD",
                                }
                            ],
                            "Warnings": [],
                        }
                    ]
                }
            },
        )

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-addres-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    # Network WAS called (the v5 service returned ok=true), but the
    # per-item error must surface as 502 — never as a silent
    # applied=True with campaign_id=None.
    assert network_called["calls"] == 1
    assert response.status_code == 502, response.text
    body = response.json()
    detail = body.get("detail")
    assert isinstance(detail, dict), body
    assert detail.get("error_type") == "YandexDirectError"
    # The audit MUST reflect the failed apply, never an applied one.
    assert "TOPSECRET-LC-ADDRES" not in response.text
    failed_events = [
        e
        for e in store.audit_events
        if e.action == "live_create_campaign_failed"
    ]
    applied_events = [
        e
        for e in store.audit_events
        if e.action == "live_create_campaign_requested"
        and (e.details or {}).get("campaign_id") is not None
    ]
    assert failed_events, "expected live_create_campaign_failed audit event"
    assert not applied_events, (
        "must NOT record a campaign_id for a failed AddResults item"
    )
    last = failed_events[-1]
    yandex_error = (last.details or {}).get("yandex_error", "")
    # The reason must reference the per-item error code, not just
    # the envelope shape.
    assert "AddResults" in yandex_error or "1520" in yandex_error


def test_live_create_store_value_error_surfaces_as_409_not_500():
    """If a direct store caller (e.g. an integration test, a future
    background worker, or a unit test that exercises the store
    directly) triggers the store's ``ValueError`` for an unapproved
    apply, the endpoint MUST surface it as 409 — never as the
    opaque FastAPI 500 default, and never as a 502 mis-classified
    as ``YandexDirectError``.

    The endpoint's ``except ValueError`` branch sits before the
    generic ``except YandexDirectError`` / ``except Exception``
    safety nets so the contract is honoured even when the endpoint
    gate is bypassed.
    """
    settings = _settings("live_write", token="TOPSECRET-LC-VALUE")
    yandex = _client_with_handler(
        settings, lambda r: httpx.Response(200, json={"result": {}})
    )
    draft_id = _create_draft()
    # Build an unapproved payload — the endpoint gate normally
    # short-circuits with HTTP 409 for ``approved=False``, so the
    # store is unreachable via HTTP. We exercise the store's
    # ``ValueError`` directly to pin the endpoint's contract for
    # direct callers.
    from app.models import LiveCreateCampaignRequest

    payload = LiveCreateCampaignRequest(
        draft_id=draft_id,
        approved=False,
        idempotency_key="live-create-value-001",
        dry_run=False,
    )
    # Direct store call bypasses the endpoint gate.
    from app.store import store

    raised: Exception | None = None
    try:
        store.live_create_campaign(
            payload, settings=settings, client=yandex
        )
    except ValueError as exc:
        raised = exc
    assert raised is not None
    assert "approval" in str(raised).lower()

    # Now exercise the HTTP path: the endpoint catches ValueError
    # and returns 409. We simulate a "leaked" ValueError by
    # monkeypatching the store helper to raise.
    def boom(*_args, **_kwargs):
        raise ValueError("Action requires explicit approval")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        from app import main as main_mod

        original = main_mod.store.live_create_campaign
        main_mod.store.live_create_campaign = boom
        try:
            response = client.post(
                "/yandex/campaigns/live-create",
                json=_live_create_payload(
                    draft_id,
                    approved=True,  # bypass the endpoint gate
                    dry_run=False,
                    idempotency_key="live-create-value-002",
                ),
            )
        finally:
            main_mod.store.live_create_campaign = original
    finally:
        app.dependency_overrides.clear()

    # MUST be 409, not 500/502. The token must NOT appear.
    assert response.status_code == 409, response.text
    assert "TOPSECRET-LC-VALUE" not in response.text


def test_live_create_apply_addresults_missing_id_treated_as_failed():
    """v5 contract: a successful AddResults item MUST include ``Id``.
    If ``Id`` is missing or null on an item with no Errors, treat it
    as a failed response — never as ``applied=True``.
    """
    settings = _settings("live_write", token="TOPSECRET-LC-NOID")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        # v5 envelope says ok, AddResults has one item, no Errors, no
        # Id — this is a mis-shaped success, treat as failure.
        return httpx.Response(200, json={"result": {"AddResults": [{"Warnings": []}]}})

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-noid-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert network_called["calls"] == 1
    assert response.status_code == 502, response.text
    body = response.json()
    detail = body.get("detail")
    assert isinstance(detail, dict), body
    assert detail.get("error_type") == "YandexDirectError"
    assert "TOPSECRET-LC-NOID" not in response.text
    failed_events = [
        e
        for e in store.audit_events
        if e.action == "live_create_campaign_failed"
    ]
    assert failed_events, "expected live_create_campaign_failed audit event"


def test_live_create_unexpected_exception_returns_502_and_audits_failed():
    """A non-typed exception from the transport MUST be converted to
    502 + audit, never the opaque FastAPI 500 default.
    """
    settings = _settings("live_write", token="TOPSECRET-LC-UNEXPECTED")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("upstream transport surprise")

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-unexpected-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502, response.text
    body = response.json()
    detail = body.get("detail")
    assert isinstance(detail, dict), body
    assert detail.get("error_type") == "YandexDirectError"
    assert "TOPSECRET-LC-UNEXPECTED" not in response.text
    assert "Traceback" not in response.text
    failed_events = [
        e
        for e in store.audit_events
        if e.action == "live_create_campaign_failed"
    ]
    assert failed_events, "expected live_create_campaign_failed audit event"


# ---------------------------------------------------------------------------
# YandexDirectClient helper
# ---------------------------------------------------------------------------


def test_yandex_direct_client_campaigns_add_posts_to_campaigns_service_with_method_add():
    """The new ``YandexDirectClient.campaigns_add`` helper MUST post
    to the v5 ``campaigns`` service with ``method=add`` and a
    ``Campaigns`` list. No token in the response."""
    settings = _settings("live_write", token="SECRET-LC-HELPER")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"result": {"AddResults": [{"Id": 7}]}},
        )

    client_obj = YandexDirectClient(
        settings=settings, transport=httpx.MockTransport(handler)
    )
    result = client_obj.campaigns_add(
        [
            {
                "Name": "test",
                "StartDate": "2026-06-15",
                "TextCampaign": {"BiddingStrategy": {"Strategy": "AVERAGE_CPC"}},
            }
        ]
    )
    assert result["ok"] is True
    assert captured["url"] == "https://api.direct.yandex.com/json/v5/campaigns"
    assert captured["body"]["method"] == "add"
    assert captured["body"]["params"]["Campaigns"][0]["Name"] == "test"
    # No token echo.
    assert "SECRET-LC-HELPER" not in str(result)


# ---------------------------------------------------------------------------
# openapi / docs smoke
# ---------------------------------------------------------------------------


def test_openapi_documents_live_create_campaign_endpoint():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    paths = spec["paths"]
    assert "/yandex/campaigns/live-create" in paths
    post = paths["/yandex/campaigns/live-create"]["post"]
    # The LiveCreateCampaignRequest schema is referenced in the
    # OpenAPI components under its model name.
    ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert "LiveCreateCampaignRequest" in ref


# ---------------------------------------------------------------------------
# Chain tests (stage 2 / stage 3 / stage 4)
#
# Stage 5 (negativekeywordsharedsets.add) is intentionally
# NOT implemented — see tests/test_live_create_chain_helpers.py.
# ---------------------------------------------------------------------------


def _populate_chain_draft(draft_id: str) -> dict[str, Any]:
    """Populate a freshly-created campaign-draft with one ad group,
    one ad, and a small set of keywords so the live-create chain
    has real inputs to translate into v5 payloads."""
    g = client.post(
        f"/campaign-drafts/{draft_id}/ad-groups",
        json={
            "name": "ремонт кондиционеров",
            "keywords": [
                "ремонт кондиционеров Казань",
                "чистка кондиционеров Казань",
            ],
        },
    )
    assert g.status_code == 200, g.text
    group_id = g.json()["ad_groups"][-1]["id"]
    # ad targeting that group
    a = client.post(
        f"/campaign-drafts/{draft_id}/ads",
        json={
            "ad_group_id": group_id,
            "title": "Заголовок объявления",
            "text": "Текст объявления",
            "landing_url": "https://example.com/landing",
        },
    )
    assert a.status_code == 200, a.text
    return {
        "group_id": group_id,
        "ad_id": a.json()["ads"][-1]["id"],
        "group_keywords": [
            "ремонт кондиционеров Казань",
            "чистка кондиционеров Казань",
        ],
    }


def test_live_create_dry_run_previews_all_four_stages_with_v5_shape():
    """A dry-run live-create MUST preview every stage that the
    apply path would call: ``campaigns.add`` (already covered by
    stage 1 tests) plus ``adgroups.add``, ``ads.add`` and
    ``keywords.add``. The preview is what an operator uses to
    confirm the chain before flipping ``dry_run=False`` — it MUST
    never touch the network, and the v5 shape MUST be the
    documented contract (no invented field names).
    """
    settings = _settings("live_readonly", token="TOPSECRET-CHAIN-DR")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 1}]}})

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()
    _populate_chain_draft(draft_id)
    # Add a campaign-level negative so the adgroups.add preview
    # exercises the negative-keyword block.
    n = client.patch(
        f"/campaign-drafts/{draft_id}/negative-keywords",
        json={"negative_keywords": ["бесплатно", "diy"]},
    )
    assert n.status_code == 200, n.text

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id, idempotency_key="live-create-chain-dr-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    # Dry-run must NEVER touch the network.
    assert network_called["calls"] == 0
    assert body["dry_run"] is True
    assert body["applied"] is False
    # The preview now shows the chained v5 payloads — one entry
    # per stage. Each entry is the full v5 service call that would
    # be made on apply (method + params).
    preview = body["payload_preview"]
    assert preview["method"] == "campaigns.add"
    chain = preview["params"].get("chain")
    assert isinstance(chain, list)
    methods = [entry["method"] for entry in chain]
    assert methods == ["adgroups.add", "ads.add", "keywords.add"]
    # Stage 2 preview uses the v5 AdGroups shape.
    ag = chain[0]
    assert ag["params"]["AdGroups"][0]["CampaignId"] is None  # not known until apply
    assert ag["params"]["AdGroups"][0]["NegativeKeywords"] == {"Items": ["бесплатно", "diy"]}
    # Stage 3 preview uses the v5 Ads shape.
    assert chain[1]["params"]["Ads"][0]["TextAd"]["Title"] == "Заголовок объявления"
    # Stage 4 preview is a flat list of {Keyword, AdGroupId} placeholders.
    assert chain[2]["params"]["Keywords"][0]["Keyword"].startswith("ремонт")
    # Stage 5 is still NOT performed.
    assert "negativekeywordsharedsets.add" in body["not_implemented"]
    assert body["stages_executed"] == []


def test_live_create_apply_chains_calls_in_order_with_ids_mapped():
    """``live_write`` + ``approved`` + ``idempotency_key`` +
    ``dry_run=False`` MUST dispatch the four v5 calls IN ORDER
    (campaigns.add → adgroups.add → ads.add → keywords.add) and
    the response MUST surface the new ids (campaign_id,
    ad_group_ids, ad_ids, keyword_ids) in the same order the
    draft items were submitted. Mapping local draft ad group ids
    to the Yandex ids returned by ``adgroups.add`` MUST be
    applied to the subsequent ads.add and keywords.add calls.
    """
    settings = _settings("live_write", token="TOPSECRET-CHAIN-APPLY")
    captured: dict[str, Any] = {
        "urls": [],
        "bodies": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content.decode())
        captured["urls"].append(url)
        captured["bodies"].append(body)
        # Return distinct ids per stage so we can pin the mapping.
        if url.endswith("/campaigns"):
            return httpx.Response(
                200,
                json={"result": {"AddResults": [{"Id": 9000, "Warnings": []}]}},
            )
        if url.endswith("/adgroups"):
            return httpx.Response(
                200,
                json={"result": {"AddResults": [{"Id": 90001}]}},
            )
        if url.endswith("/ads"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 80001}]}}
            )
        if url.endswith("/keywords"):
            return httpx.Response(
                200,
                json={"result": {"AddResults": [
                    {"Id": 70001}, {"Id": 70002}, {"Id": 70003}, {"Id": 70004}
                ]}},
            )
        raise AssertionError(f"unexpected url: {url}")

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-chain-apply-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["dry_run"] is False
    # Four v5 calls, in order.
    assert captured["urls"] == [
        "https://api.direct.yandex.com/json/v5/campaigns",
        "https://api.direct.yandex.com/json/v5/adgroups",
        "https://api.direct.yandex.com/json/v5/ads",
        "https://api.direct.yandex.com/json/v5/keywords",
    ]
    # Stage 1 body.
    assert captured["bodies"][0]["method"] == "add"
    assert "Status" not in captured["bodies"][0]["params"]["Campaigns"][0]
    # Stage 2 body — CampaignId is the new Yandex campaign id, not
    # the local draft id. We have one ad group with 2 keywords.
    assert captured["bodies"][1]["params"]["AdGroups"][0]["CampaignId"] == 9000
    # Stage 3 body — AdGroupId is the Yandex ad group id returned
    # by adgroups.add (90001), NOT the local draft ad group id.
    assert captured["bodies"][2]["params"]["Ads"][0]["AdGroupId"] == 90001
    assert captured["bodies"][2]["params"]["Ads"][0]["TextAd"]["Href"] == (
        "https://example.com/landing"
    )
    # Stage 4 body — AdGroupId is also the Yandex ad group id.
    for kw in captured["bodies"][3]["params"]["Keywords"]:
        assert kw["AdGroupId"] == 90001
    # Response includes all the new ids, in order, with no token.
    assert body["campaign_id"] == "9000"
    assert body["ad_group_ids"] == ["90001"]
    assert body["ad_ids"] == ["80001"]
    assert len(body["keyword_ids"]) == 4
    assert body["stages_executed"] == [
        "campaigns.add",
        "adgroups.add",
        "ads.add",
        "keywords.add",
    ]
    # Stage 5 is still NOT performed.
    assert body["not_implemented"] == ["negativekeywordsharedsets.add"]
    assert "TOPSECRET-CHAIN-APPLY" not in response.text


def test_live_create_apply_idempotency_replay_avoids_duplicate_chain():
    """Replaying the same ``idempotency_key`` MUST NOT re-send any
    of the four v5 calls. The second response is the cached first
    response (same ``audit_id`` / same ids / same ``stages_executed``).
    """
    settings = _settings("live_write", token="TOPSECRET-CHAIN-IDEM")
    captured = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        url = str(request.url)
        count = 4 if url.endswith("/keywords") else 1
        return httpx.Response(
            200,
            json={"result": {"AddResults": [{"Id": captured["calls"] * 100 + i} for i in range(count)]}},
        )

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        body = _live_create_payload(
            draft_id,
            dry_run=False,
            idempotency_key="live-create-chain-idem-001",
        )
        r1 = client.post("/yandex/campaigns/live-create", json=body)
        r2 = client.post("/yandex/campaigns/live-create", json=body)
    finally:
        app.dependency_overrides.clear()

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["audit_id"] == r2.json()["audit_id"]
    assert r1.json()["campaign_id"] == r2.json()["campaign_id"]
    assert r1.json()["ad_group_ids"] == r2.json()["ad_group_ids"]
    assert r1.json()["ad_ids"] == r2.json()["ad_ids"]
    assert r1.json()["keyword_ids"] == r2.json()["keyword_ids"]
    # Four v5 calls on the first apply (campaigns, adgroups, ads, keywords);
    # zero on the replay.
    assert captured["calls"] == 4


def test_live_create_apply_dry_run_does_not_consume_real_idempotency_key():
    """A ``dry_run=True`` request MUST NOT consume the
    ``idempotency_key`` for the subsequent ``dry_run=False``
    request. The dry-run replays return the cached dry-run result;
    the apply call performs a fresh chain. This pins the safety
    property that a preview never blocks a real apply.
    """
    settings = _settings("live_write", token="TOPSECRET-CHAIN-MIX")
    captured = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        url = str(request.url)
        count = 4 if url.endswith("/keywords") else 1
        return httpx.Response(
            200,
            json={"result": {"AddResults": [{"Id": 100 + captured["calls"] + i} for i in range(count)]}},
        )

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        key = "live-create-chain-mix-001"
        dr = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(draft_id, idempotency_key=key),
        )
        apply = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id, dry_run=False, idempotency_key=key
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert dr.status_code == 200, dr.text
    assert apply.status_code == 200, apply.text
    # Dry-run was 0 network calls. Apply did 4 v5 calls (campaigns, adgroups, ads, keywords).
    assert captured["calls"] == 4
    assert dr.json()["applied"] is False
    assert apply.json()["applied"] is True


def test_live_create_apply_addresults_errors_at_adgroups_stops_chain():
    """v5 ``adgroups.add`` AddResults with ``Errors`` (or missing
    ``Id``) MUST stop the chain at stage 2: no ``ads.add``, no
    ``keywords.add`` call. The endpoint returns 502, audits
    ``live_create_campaign_failed`` with the failing stage, and the
    campaign-id is NOT returned as ``applied=True``.
    """
    settings = _settings("live_write", token="TOPSECRET-CHAIN-AGERR")
    captured: dict[str, Any] = {"urls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["urls"].append(str(request.url))
        if str(request.url).endswith("/campaigns"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 1}]}}
            )
        if str(request.url).endswith("/adgroups"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AddResults": [
                            {
                                "Errors": [
                                    {
                                        "Code": 9000,
                                        "Message": "fake adgroup error",
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
        raise AssertionError(
            f"chain continued past failure; should not have called {request.url}"
        )

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-chain-agerr-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    # Only campaigns.add and adgroups.add were called — ads.add
    # and keywords.add are NOT called after the failure.
    assert captured["urls"] == [
        "https://api.direct.yandex.com/json/v5/campaigns",
        "https://api.direct.yandex.com/json/v5/adgroups",
    ]
    assert response.status_code == 502, response.text
    body = response.json()
    assert isinstance(body.get("detail"), dict)
    assert body["detail"]["error_type"] == "YandexDirectError"
    # Token never echoed.
    assert "TOPSECRET-CHAIN-AGERR" not in response.text
    # The audit MUST reflect the failed apply.
    failed_events = [
        e
        for e in store.audit_events
        if e.action == "live_create_campaign_failed"
    ]
    assert failed_events, "expected live_create_campaign_failed audit event"
    last = failed_events[-1]
    yandex_error = (last.details or {}).get("yandex_error", "")
    assert "adgroups.add" in yandex_error
    # An applied=True audit with a campaign id MUST NOT be recorded.
    applied = [
        e
        for e in store.audit_events
        if e.action == "live_create_campaign_requested"
        and (e.details or {}).get("applied") is True
    ]
    assert not applied, (
        "must NOT record an applied=True audit when chain fails at stage 2"
    )


def test_live_create_apply_addresults_missing_id_at_ads_stops_chain():
    """Missing ``Id`` on the ``ads.add`` AddResults item MUST stop
    the chain at stage 3. The endpoint returns 502, audits
    ``live_create_campaign_failed`` with the failing stage, and
    ``keywords.add`` is NOT called.
    """
    settings = _settings("live_write", token="TOPSECRET-CHAIN-ADNOID")
    captured: dict[str, Any] = {"urls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["urls"].append(str(request.url))
        if str(request.url).endswith("/campaigns"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 1}]}}
            )
        if str(request.url).endswith("/adgroups"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 100}]}}
            )
        if str(request.url).endswith("/ads"):
            # ok envelope, but no Id on the AddResults item.
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Warnings": []}]}}
            )
        raise AssertionError(
            f"chain continued past failure; should not have called {request.url}"
        )

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-chain-adnoid-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    # ads.add was the failing stage — keywords.add must NOT be called.
    assert captured["urls"] == [
        "https://api.direct.yandex.com/json/v5/campaigns",
        "https://api.direct.yandex.com/json/v5/adgroups",
        "https://api.direct.yandex.com/json/v5/ads",
    ]
    assert response.status_code == 502, response.text
    assert "TOPSECRET-CHAIN-ADNOID" not in response.text
    failed = [e for e in store.audit_events if e.action == "live_create_campaign_failed"]
    assert failed
    assert "ads.add" in (failed[-1].details or {}).get("yandex_error", "")


def test_live_create_apply_addresults_errors_at_keywords_stops_chain():
    """Errors on the ``keywords.add`` AddResults item MUST fail the
    chain at stage 4. The endpoint returns 502, the audit log
    records the failing stage, and no further calls are made.
    """
    settings = _settings("live_write", token="TOPSECRET-CHAIN-KWERR")
    captured: dict[str, Any] = {"urls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["urls"].append(str(request.url))
        if str(request.url).endswith("/campaigns"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 1}]}}
            )
        if str(request.url).endswith("/adgroups"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 100}]}}
            )
        if str(request.url).endswith("/ads"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 200}]}}
            )
        if str(request.url).endswith("/keywords"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AddResults": [
                            {
                                "Errors": [
                                    {
                                        "Code": 9100,
                                        "Message": "fake keyword error",
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
        raise AssertionError(f"unexpected url: {request.url}")

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-chain-kwerr-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert captured["urls"] == [
        "https://api.direct.yandex.com/json/v5/campaigns",
        "https://api.direct.yandex.com/json/v5/adgroups",
        "https://api.direct.yandex.com/json/v5/ads",
        "https://api.direct.yandex.com/json/v5/keywords",
    ]
    assert response.status_code == 502, response.text
    assert "TOPSECRET-CHAIN-KWERR" not in response.text
    failed = [e for e in store.audit_events if e.action == "live_create_campaign_failed"]
    assert failed
    assert "keywords.add" in (failed[-1].details or {}).get("yandex_error", "")


def test_live_create_apply_unexpected_transport_exception_returns_502():
    """An unexpected non-typed exception from the transport during
    the chain MUST be converted to 502 with a redacted message —
    never the opaque FastAPI 500 default, and never a token echo.
    """
    settings = _settings("live_write", token="TOPSECRET-CHAIN-BOOM")

    def handler(request: httpx.Request) -> httpx.Response:
        # First two stages succeed; stage 3 transport throws.
        if str(request.url).endswith("/campaigns"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 1}]}}
            )
        if str(request.url).endswith("/adgroups"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 100}]}}
            )
        raise RuntimeError("upstream transport surprise during chain")

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-chain-boom-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502, response.text
    body = response.json()
    detail = body.get("detail")
    assert isinstance(detail, dict)
    assert detail.get("error_type") == "YandexDirectError"
    assert "TOPSECRET-CHAIN-BOOM" not in response.text
    assert "Traceback" not in response.text
    failed = [e for e in store.audit_events if e.action == "live_create_campaign_failed"]
    assert failed


def test_live_create_apply_with_empty_draft_creates_only_campaign():
    """The intentional behaviour for an empty draft: the chain
    performs only ``campaigns.add`` and returns a new Yandex
    campaign id; ``adgroups.add`` / ``ads.add`` / ``keywords.add``
    are SKIPPED (no empty list sent to v5, which the v5 service
    rejects). The response still carries the full ``not_implemented``
    list (``negativekeywordsharedsets.add``) and a smaller
    ``stages_executed``.
    """
    settings = _settings("live_write", token="TOPSECRET-CHAIN-EMPTY")
    captured: dict[str, Any] = {"urls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["urls"].append(str(request.url))
        # Only campaigns.add is called; any other URL is a regression.
        if str(request.url).endswith("/campaigns"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 55555}]}}
            )
        raise AssertionError(
            f"chain should have skipped stage; got {request.url}"
        )

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()  # no ad groups, no ads, no keywords

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-chain-empty-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert captured["urls"] == [
        "https://api.direct.yandex.com/json/v5/campaigns",
    ]
    assert body["applied"] is True
    assert body["campaign_id"] == "55555"
    assert body["ad_group_ids"] == []
    assert body["ad_ids"] == []
    assert body["keyword_ids"] == []
    assert body["stages_executed"] == ["campaigns.add"]
    assert body["not_implemented"] == ["negativekeywordsharedsets.add"]


def test_live_create_apply_preserves_ad_href_text_title_with_no_token_leak():
    """The ``ads.add`` payload MUST preserve the draft ad's
    ``title`` / ``text`` / ``landing_url`` (Href) — no token
    leak, no field rename.
    """
    settings = _settings("live_write", token="TOPSECRET-CHAIN-ADLEAK")
    captured: dict[str, Any] = {"ads_body": None}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if str(request.url).endswith("/campaigns"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 1}]}}
            )
        if str(request.url).endswith("/adgroups"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 100}]}}
            )
        if str(request.url).endswith("/ads"):
            captured["ads_body"] = body
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 200}]}}
            )
        if str(request.url).endswith("/keywords"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 300}, {"Id": 301}, {"Id": 302}, {"Id": 303}]}}
            )
        raise AssertionError(f"unexpected url: {request.url}")

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-chain-adleak-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = captured["ads_body"]
    assert body["params"]["Ads"][0]["TextAd"]["Title"] == "Заголовок объявления"
    assert body["params"]["Ads"][0]["TextAd"]["Text"] == "Текст объявления"
    assert body["params"]["Ads"][0]["TextAd"]["Href"] == "https://example.com/landing"
    # Token never appears in any chain body.
    chain_bodies = json.dumps(captured, ensure_ascii=False)
    assert "TOPSECRET-CHAIN-ADLEAK" not in chain_bodies


def test_live_create_apply_includes_draft_negative_keywords_in_adgroups_add():
    """Group-level ``NegativeKeywords.Items`` MUST carry the
    draft's ``negative_keywords`` (the same value the operator
    sees in the UI). The shape is the v5 ``adgroups.add``
    NegativeKeywords block — mirrors the existing
    ``adgroups.update`` semantic-change path.
    """
    settings = _settings("live_write", token="TOPSECRET-CHAIN-NEG")
    captured: dict[str, Any] = {"adgroups_body": None}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if str(request.url).endswith("/campaigns"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 1}]}}
            )
        if str(request.url).endswith("/adgroups"):
            captured["adgroups_body"] = body
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 100}]}}
            )
        if str(request.url).endswith("/ads"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 200}]}}
            )
        if str(request.url).endswith("/keywords"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 300}, {"Id": 301}, {"Id": 302}, {"Id": 303}]}}
            )
        raise AssertionError(f"unexpected url: {request.url}")

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()
    _populate_chain_draft(draft_id)
    n = client.patch(
        f"/campaign-drafts/{draft_id}/negative-keywords",
        json={"negative_keywords": ["бесплатно", "diy"]},
    )
    assert n.status_code == 200, n.text

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-chain-neg-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = captured["adgroups_body"]
    assert body["params"]["AdGroups"][0]["NegativeKeywords"] == {
        "Items": ["бесплатно", "diy"]
    }


def test_live_create_apply_rejected_in_sandbox_and_mock_before_network():
    """The mode gate MUST be no-network for ``sandbox`` and
    ``mock`` even on a full chain. This re-pins the existing
    stage-1 gate contract for the chain path so a future
    maintainer cannot accidentally bypass the gate when extending
    the chain.
    """
    for mode, token in [("sandbox", "TOPSECRET-CHAIN-SB"), ("mock", "TOPSECRET-CHAIN-MK")]:
        settings = _settings(mode, token=token)
        network_called = {"calls": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            network_called["calls"] += 1
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 1}]}}
            )

        yandex = _client_with_handler(settings, handler)
        draft_id = _create_draft()
        _populate_chain_draft(draft_id)

        app.dependency_overrides[get_settings] = (lambda settings=settings: lambda: settings)()
        app.dependency_overrides[get_yandex_client] = (lambda yandex=yandex: lambda: yandex)()
        try:
            response = client.post(
                "/yandex/campaigns/live-create",
                json=_live_create_payload(
                    draft_id,
                    dry_run=False,
                    idempotency_key=f"live-create-chain-gate-{mode}-001",
                ),
            )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 409, response.text
        assert mode in response.text.lower()
        assert token not in response.text
        assert network_called["calls"] == 0


def test_openapi_documents_live_create_keyword_ids_field():
    """The OpenAPI snapshot MUST document the new ``keyword_ids``
    field on ``LiveCreateCampaignResult`` so external API
    consumers can rely on it.
    """
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    result_schema = spec["components"]["schemas"]["LiveCreateCampaignResult"]
    assert "keyword_ids" in result_schema["properties"], (
        "keyword_ids must be documented in OpenAPI for live-create consumers"
    )
    kw_ids = result_schema["properties"]["keyword_ids"]
    assert kw_ids["type"] == "array"
    assert kw_ids["items"]["type"] == "string"


# ---------------------------------------------------------------------------
# RegionIds (reviewer REQUEST_CHANGES blocker)
#
# The reviewer pinned the absence of ``RegionIds`` in the adgroups.add
# payload as a hard blocker. The dry-run must surface RegionIds so the
# operator can see the resolved ids; the apply must reject unknown
# regions BEFORE any campaigns.add network call.
# ---------------------------------------------------------------------------


def test_live_create_dry_run_previews_region_ids_in_adgroups_add():
    """A dry-run live-create MUST surface ``RegionIds`` in the
    ``adgroups.add`` preview so the operator can confirm the geo
    target before flipping ``dry_run=False``. The ids MUST come
    from the explicit local resolver (``_resolve_region_to_ids``)
    keyed off the draft's ``region`` field — Казань → [43]."""
    settings = _settings("live_readonly", token="TOPSECRET-CHAIN-REGION-DR")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 1}]}})

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()  # defaults to region="Казань"
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id, idempotency_key="live-create-chain-region-dr-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    # Dry-run must NEVER touch the network.
    assert network_called["calls"] == 0
    chain = body["payload_preview"]["params"]["chain"]
    ag = chain[0]["params"]["AdGroups"][0]
    assert ag["RegionIds"] == [43], (
        "adgroups.add preview MUST carry RegionIds resolved from draft.region; "
        "Казань must map to [43]."
    )


def test_live_create_apply_includes_region_ids_in_adgroups_add():
    """A real apply (live_write + approved + idempotency_key) MUST
    send ``RegionIds`` in the actual ``adgroups.add`` HTTP body
    that the chain dispatches to Yandex Direct. This is the
    reviewer's hard blocker: missing RegionIds = adgroups.add
    rejection."""
    settings = _settings("live_write", token="TOPSECRET-CHAIN-REGION-APPLY")
    captured: dict[str, Any] = {"adgroups_body": None}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if str(request.url).endswith("/campaigns"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 7777}]}}
            )
        if str(request.url).endswith("/adgroups"):
            captured["adgroups_body"] = body
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 8888}]}}
            )
        if str(request.url).endswith("/ads"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 9999}]}}
            )
        if str(request.url).endswith("/keywords"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AddResults": [
                            {"Id": 1001},
                            {"Id": 1002},
                            {"Id": 1003},
                            {"Id": 1004},
                        ]
                    }
                },
            )
        raise AssertionError(f"unexpected url: {request.url}")

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()  # region="Казань"
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-chain-region-apply-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = captured["adgroups_body"]
    assert body["params"]["AdGroups"][0]["RegionIds"] == [43]
    # The exact Yandex campaign id from stage 1 must be substituted.
    assert body["params"]["AdGroups"][0]["CampaignId"] == 7777
    # No token echo anywhere.
    assert "TOPSECRET-CHAIN-REGION-APPLY" not in response.text


def test_live_create_apply_rejects_unknown_region_before_network():
    """When the draft's region is not in the local resolver, the
    apply path MUST reject the request BEFORE any campaigns.add
    network call. The endpoint surfaces the typed error as a
    502 with a redacted message that names the offending region
    so the operator can fix it."""
    settings = _settings("live_write", token="TOPSECRET-CHAIN-REGION-UNMAPPED")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 1}]}})

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()  # region="Казань" by default
    # Force a region the resolver does NOT know.
    bad = client.patch(
        f"/campaign-drafts/{draft_id}", json={"region": "Тмутаракань"}
    )
    assert bad.status_code == 200, bad.text
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-chain-region-unmapped-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    # NO network call was made — the gate is no-network.
    assert network_called["calls"] == 0, (
        "Unknown region must be rejected before any v5 network call."
    )
    # Endpoint surfaces the typed error as 502.
    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    # Detail is a dict with redacted message.
    msg = detail.get("message") if isinstance(detail, dict) else str(detail)
    assert "Тмутаракань" in msg, (
        "The 502 message MUST name the offending region so the operator "
        "can fix it."
    )
    assert "TOPSECRET-CHAIN-REGION-UNMAPPED" not in response.text

    # The audit log records the failure with the typed error and
    # the offending region — no token leak.
    failed_events = [
        e
        for e in store.audit_events
        if e.action == "live_create_campaign_failed"
    ]
    assert failed_events, "expected a live_create_campaign_failed audit event"
    assert "Тмутаракань" in (failed_events[-1].details or {}).get(
        "yandex_error", ""
    ) or "Тмутаракань" in str(failed_events[-1].details)
    assert "TOPSECRET-CHAIN-REGION-UNMAPPED" not in str(
        failed_events[-1].details
    )


def test_live_create_apply_no_status_draft_in_stage1_or_adgroups_payload():
    """Defence-in-depth: the apply path must NOT send
    ``Status="DRAFT"`` (or any other ``Status`` value) on either
    the ``campaigns.add`` or the ``adgroups.add`` v5 payloads.
    Lifecycle is controlled by Direct and the separate resume
    endpoint, not by the create chain. This pins the surface
    area in the apply path (the chain-builder unit test covers
    the helper output)."""
    settings = _settings("live_write", token="TOPSECRET-CHAIN-NOSTATUS")
    captured: dict[str, Any] = {"campaigns": None, "adgroups": None}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if str(request.url).endswith("/campaigns"):
            captured["campaigns"] = body
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 1}]}}
            )
        if str(request.url).endswith("/adgroups"):
            captured["adgroups"] = body
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 2}]}}
            )
        if str(request.url).endswith("/ads"):
            return httpx.Response(
                200, json={"result": {"AddResults": [{"Id": 3}]}}
            )
        if str(request.url).endswith("/keywords"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AddResults": [
                            {"Id": 4},
                            {"Id": 5},
                            {"Id": 6},
                            {"Id": 7},
                        ]
                    }
                },
            )
        raise AssertionError(f"unexpected url: {request.url}")

    yandex = _client_with_handler(settings, handler)
    draft_id = _create_draft()
    _populate_chain_draft(draft_id)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/live-create",
            json=_live_create_payload(
                draft_id,
                dry_run=False,
                idempotency_key="live-create-chain-nostatus-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    # Stage 1: no Status field on the v5 campaign.
    assert "Status" not in captured["campaigns"]["params"]["Campaigns"][0]
    # Stage 2: no Status field on the v5 ad group.
    for ag in captured["adgroups"]["params"]["AdGroups"]:
        assert "Status" not in ag, (
            "adgroups.add items MUST NOT carry a Status field."
        )


# ---------------------------------------------------------------------------
# fixture cleanup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_store():
    """Reset the in-memory store between tests so audit counts are
    stable and the live-create idempotency cache is empty."""
    store.audit_events.clear()
    for attr in (
        "semantic_packages_by_id",
        "semantic_apply_results_by_key",
        "live_create_results_by_key",
    ):
        if hasattr(store, attr):
            getattr(store, attr).clear()
    yield
    store.audit_events.clear()
    for attr in (
        "semantic_packages_by_id",
        "semantic_apply_results_by_key",
        "live_create_results_by_key",
    ):
        if hasattr(store, attr):
            getattr(store, attr).clear()
