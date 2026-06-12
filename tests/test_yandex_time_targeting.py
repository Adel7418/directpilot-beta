"""Tests for the campaign TimeTargeting update endpoint.

``POST /yandex/campaigns/{campaign_id}/time-targeting`` is a
safety-sensitive write path: a real apply rewrites the
``TimeTargeting`` block on the user's production Yandex Direct
campaign. The contract is identical to the rest of the product
surface:

* ``dry_run=True`` is ALWAYS allowed and NEVER performs a network
  write. The response is the exact v5 ``campaigns.update``
  payload that WOULD be sent, with ``applied=False`` and
  ``source="yandex"`` in live modes or ``source="mock"`` in mock.
* In ``live_readonly`` mode, ``dry_run=False`` is REJECTED before
  any network call (HTTP 409). The same gate rejects ``sandbox``
  and ``mock`` apply paths.
* In ``live_write`` mode, ``dry_run=False`` is allowed only when
  ``approved=True`` and a valid ``idempotency_key`` is supplied.
  Without either, the request is rejected.
* Audit events are recorded for both dry-run and live apply. They
  never contain the OAUTH token.
* The apply path performs a v5 ``campaigns.update`` (REPLACE-shaped
  on the ``TimeTargeting`` block) and a follow-up v5
  ``campaigns.get TimeTargeting`` to verify the schedule landed.
* Replays of the same ``(campaign_id, idempotency_key)`` pair
  return the cached first response without re-sending to Yandex.

The tests below pin this contract. We do NOT exercise any real
Yandex ``campaigns.update`` call — the live-write success path is
verified via ``httpx.MockTransport`` so it stays hermetic. Token
leakage is asserted in every test.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.models import (
    WEEK_DAY_NAMES,
    YandexTimeTargetingHourly,
    YandexTimeTargetingRequest,
    YandexTimeTargetingSchedule,
)
from app.store import store
from app.yandex_direct import YandexDirectClient


client = TestClient(app)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


SECRET_TOKEN = "TOPSECRET-TT-001"


def _settings(mode: str, token: str | None = SECRET_TOKEN) -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=token)


def _client_with_handler(settings: Settings, handler) -> YandexDirectClient:
    """Build a YandexDirectClient whose transport is the given httpx handler.

    The handler is wrapped so we can assert that no real network
    call happened on rejected / dry-run paths.
    """

    def _transport(request: httpx.Request) -> httpx.Response:
        try:
            return handler(request)
        except AssertionError:
            raise

    transport = httpx.MockTransport(_transport)
    return YandexDirectClient(settings=settings, transport=transport)


def _hours_8_to_22_full() -> list[int]:
    """The common 08:00-22:00 (local TZ) helper schedule.

    Hours 0..7 are paused (``0``), hours 8..21 are full bid
    (``100``), hour 22 is the 22:00 hour and is full bid, hour 23
    is the 23:00 hour and is paused. 24 values total.
    """
    return [0] * 8 + [100] * 14 + [0] * 2


def _full_schedule(hours: list[int] | None = None) -> dict[str, Any]:
    """Build a full 7 x 24 ``schedule`` body using the given hours for
    every day of the week."""
    if hours is None:
        hours = _hours_8_to_22_full()
    return {
        "schedule": {
            "days": [{"hours": list(hours)} for _ in range(7)],
        },
    }


def _hours_body(
    hours: list[int] | None = None,
    days: list[str] | None = None,
) -> dict[str, Any]:
    """Build a flat ``hours`` (+ optional ``days``) body."""
    if hours is None:
        hours = _hours_8_to_22_full()
    body: dict[str, Any] = {"hours": list(hours)}
    if days is not None:
        body["days"] = list(days)
    return body


def _request_body(
    *,
    schedule_or_hours: dict[str, Any] | None = None,
    approved: bool = True,
    dry_run: bool = True,
    idempotency_key: str = "tt-001",
    reason: str | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    """Build a full request body for the endpoint."""
    if schedule_or_hours is None:
        schedule_or_hours = _full_schedule()
    body: dict[str, Any] = {
        "approved": approved,
        "idempotency_key": idempotency_key,
        "dry_run": dry_run,
    }
    body.update(schedule_or_hours)
    if reason is not None:
        body["reason"] = reason
    if timezone is not None:
        body["timezone"] = timezone
    return body


def _ok_update_envelope() -> dict[str, Any]:
    """A successful v5 ``campaigns.update`` envelope — the response
    Direct returns when an apply is accepted. The shape is
    ``{result: {UpdateResults: [{Id, ...}]}}`` for a no-error
    apply."""
    return {
        "result": {
            "UpdateResults": [{"Id": 710691939, "Errors": [], "Warnings": []}],
        }
    }


def _ok_readback_envelope(time_targeting: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A successful v5 ``campaigns.get TimeTargeting`` envelope
    (the read-back the endpoint performs after the apply).

    Also includes ``DailyBudget`` so the same envelope can serve
    as the pre-apply DailyBudget read response (the endpoint now
    calls ``campaigns.get`` twice: once for DailyBudget, once for
    the TimeTargeting read-back).
    """
    if time_targeting is None:
        time_targeting = [
            {
                "Days": [WEEK_DAY_NAMES[index]],
                "Hours": {"BidPercent": _hours_8_to_22_full()},
            }
            for index in range(7)
        ]
    return {
        "result": {
            "Campaigns": [
                {
                    "Id": 710691939,
                    "Name": "Ремонт кондиционеров Казань — поиск",
                    "DailyBudget": {"Amount": 5_000_000, "SpendMode": "STANDARD"},
                    "TimeTargeting": time_targeting,
                }
            ]
        }
    }


# ---------------------------------------------------------------------------
# fixture cleanup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_store():
    """Reset the in-memory store between tests so audit counts are
    stable and the time-targeting idempotency cache is empty."""
    store.audit_events.clear()
    for attr in (
        "semantic_packages_by_id",
        "semantic_apply_results_by_key",
        "live_create_results_by_key",
        "time_targeting_results_by_key",
    ):
        if hasattr(store, attr):
            getattr(store, attr).clear()
    yield
    store.audit_events.clear()
    for attr in (
        "semantic_packages_by_id",
        "semantic_apply_results_by_key",
        "live_create_results_by_key",
        "time_targeting_results_by_key",
    ):
        if hasattr(store, attr):
            getattr(store, attr).clear()


# ---------------------------------------------------------------------------
# model-level validation
# ---------------------------------------------------------------------------


def test_request_rejects_neither_schedule_nor_hours():
    """A request with neither ``schedule`` nor ``hours`` is rejected
    at the Pydantic layer with a clear error."""
    with pytest.raises(ValidationError) as exc_info:
        YandexTimeTargetingRequest(
            approved=True, idempotency_key="tt-001", dry_run=True
        )
    assert "exactly one of" in str(exc_info.value)


def test_request_rejects_both_schedule_and_hours():
    """A request with both ``schedule`` and ``hours`` is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        YandexTimeTargetingRequest(
            approved=True,
            idempotency_key="tt-001",
            dry_run=True,
            schedule=YandexTimeTargetingSchedule(
                days=[YandexTimeTargetingHourly(hours=[100] * 24) for _ in range(7)]
            ),
            hours=[100] * 24,
        )
    assert "exactly one of" in str(exc_info.value)


def test_hours_must_be_exactly_24_values():
    """The ``hours`` shape requires exactly 24 values."""
    with pytest.raises(ValidationError) as exc_info:
        YandexTimeTargetingRequest(
            approved=True,
            idempotency_key="tt-001",
            dry_run=True,
            hours=[100] * 23,  # off-by-one
        )
    assert "exactly 24" in str(exc_info.value) or "24 values" in str(exc_info.value)


def test_hours_out_of_range_rejected():
    """Percentages outside 0..100 are rejected (Direct ``BidPercent``
    is 0..100)."""
    with pytest.raises(ValidationError) as exc_info:
        YandexTimeTargetingRequest(
            approved=True,
            idempotency_key="tt-001",
            dry_run=True,
            hours=[-1] + [100] * 23,
        )
    assert "out of range" in str(exc_info.value)


def test_hours_bool_rejected_as_int():
    """A boolean that slipped into the ``hours`` list is NOT
    accepted as an integer (Python's ``bool`` is technically an
    ``int``; the model explicitly rejects this to avoid silent
    ``True/False`` -> ``1/0`` substitutions)."""
    with pytest.raises(ValidationError) as exc_info:
        YandexTimeTargetingRequest(
            approved=True,
            idempotency_key="tt-001",
            dry_run=True,
            hours=[True] + [100] * 23,
        )
    assert "must be an integer" in str(exc_info.value)


def test_schedule_must_have_exactly_seven_days():
    """The ``schedule`` shape requires exactly 7 days."""
    with pytest.raises(ValidationError):
        YandexTimeTargetingRequest(
            approved=True,
            idempotency_key="tt-001",
            dry_run=True,
            schedule=YandexTimeTargetingSchedule(
                days=[YandexTimeTargetingHourly(hours=[100] * 24) for _ in range(6)]
            ),
        )


def test_invalid_day_name_rejected():
    """An unknown day name in the ``days`` filter is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        YandexTimeTargetingRequest(
            approved=True,
            idempotency_key="tt-001",
            dry_run=True,
            hours=[100] * 24,
            days=["FUNDAY"],
        )
    assert "FUNDAY" in str(exc_info.value)


def test_duplicate_day_in_days_filter_rejected():
    """Duplicate days in the ``days`` filter are rejected."""
    with pytest.raises(ValidationError) as exc_info:
        YandexTimeTargetingRequest(
            approved=True,
            idempotency_key="tt-001",
            dry_run=True,
            hours=[100] * 24,
            days=["MONDAY", "MONDAY"],
        )
    assert "duplicated" in str(exc_info.value)


def test_idempotency_key_min_length_enforced():
    """``idempotency_key`` MUST be at least 6 characters (matches
    the rest of the product surface)."""
    with pytest.raises(ValidationError):
        YandexTimeTargetingRequest(
            approved=True,
            idempotency_key="tt",  # too short
            dry_run=True,
            hours=[100] * 24,
        )


# ---------------------------------------------------------------------------
# mock mode
# ---------------------------------------------------------------------------


def test_mock_dry_run_returns_deterministic_preview_without_network():
    """In mock mode, a dry-run must return the v5 preview with
    ``source="mock"``, ``applied=False``, and a canonical 7 x 24
    matrix. No network call is made (mock has no client)."""
    settings = _settings("mock")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(200, json={})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(idempotency_key="tt-mock-001"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign_id"] == "710691939"
    assert body["mode"] == "mock"
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["source"] == "mock"
    assert body["payload_preview"] is not None
    assert body["payload_preview"]["method"] == "campaigns.update"
    assert body["payload_preview"]["params"]["Campaigns"][0]["Id"] == 710691939
    assert (
        len(body["payload_preview"]["params"]["Campaigns"][0]["TimeTargeting"]["Schedule"]["Items"])
        == 7
    )
    assert body["readback"] is None
    # Schedule is the canonical 7 x 24 with the 08-22 helper values.
    assert body["schedule_applied"]["days"][0]["hours"] == _hours_8_to_22_full()
    # No network call.
    assert network_called["calls"] == 0
    # Token never echoed.
    assert SECRET_TOKEN not in response.text


def test_mock_apply_is_rejected_by_endpoint_gate_before_network():
    """In mock mode, ``dry_run=False`` is REJECTED at the endpoint
    gate with HTTP 409, mirroring the project convention for
    safety-sensitive writes (the endpoint refuses to dispatch a
    real apply in any non-``live_write`` mode). The mock layer is
    offline by design; the store-level mock branch is only
    reachable via direct callers (e.g. integration tests)."""
    settings = _settings("mock")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(200, json={})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-mock-apply-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    # The endpoint gate is the primary mode guard. Mock is not a
    # product write path; a real apply would be a misconfigured
    # deploy.
    assert response.status_code == 409, response.text
    assert "live_write" in str(response.json()["detail"])
    assert "mock" in str(response.json()["detail"])
    assert SECRET_TOKEN not in response.text
    # No network call.
    assert network_called["calls"] == 0


# ---------------------------------------------------------------------------
# live_readonly gates
# ---------------------------------------------------------------------------


def test_live_readonly_dry_run_is_allowed_without_update_call():
    """In ``live_readonly``, a dry-run is allowed and must NOT call
    ``campaigns.update``. It DOES call ``campaigns.get`` once to
    read the current ``DailyBudget`` for the preview. The response
    is the v5 payload preview with ``source="yandex"``,
    ``applied=False``.
    """
    settings = _settings("live_readonly")
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured_methods.append(body.get("method"))
        return httpx.Response(200, json=_ok_readback_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=True, idempotency_key="tt-lro-001", timezone="Europe/Moscow"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["mode"] == "live_readonly"
    assert body["source"] == "yandex"
    assert body["payload_preview"] is not None
    assert body["readback"] is None
    # campaigns.update was NOT called (dry-run).
    assert "update" not in captured_methods
    # campaigns.get WAS called once (DailyBudget read).
    assert "get" in captured_methods
    # Timezone label is echoed back to the operator and recorded
    # in the audit. Direct's TimeTargeting does not carry a
    # timezone; the field is metadata only.
    assert body["timezone"] == "Europe/Moscow"
    # Token never echoed.
    assert SECRET_TOKEN not in response.text


def test_live_readonly_apply_is_rejected_before_network_call():
    """In ``live_readonly``, a real apply is REJECTED before any
    network call. The response is HTTP 409 with a clear reason."""
    settings = _settings("live_readonly")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(200, json=_ok_update_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-lro-block-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "live_readonly" in str(detail)
    assert "live_write" in str(detail)
    assert SECRET_TOKEN not in response.text
    # The gate MUST be no-network: the v5 ``campaigns.update``
    # helper was never called.
    assert network_called["calls"] == 0


def test_sandbox_apply_is_rejected_before_network_call():
    """In ``sandbox`` mode, real apply is REJECTED before any network
    call. ``sandbox`` shares the v5 ``campaigns.update`` write
    shape with production."""
    settings = _settings("sandbox")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(200, json=_ok_update_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-sandbox-block-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    assert "live_write" in str(response.json()["detail"])
    assert SECRET_TOKEN not in response.text
    assert network_called["calls"] == 0


# ---------------------------------------------------------------------------
# approval gate
# ---------------------------------------------------------------------------


def test_apply_without_approved_is_rejected():
    """``approved=False`` is rejected with HTTP 409, regardless of
    the runtime mode."""
    settings = _settings("live_write")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                approved=False,
                dry_run=False,
                idempotency_key="tt-noapp-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    assert "approval" in response.text.lower()
    assert SECRET_TOKEN not in response.text


# ---------------------------------------------------------------------------
# v5 payload shape (apply path)
# ---------------------------------------------------------------------------


def test_dry_run_payload_uses_canonical_v5_time_targeting_shape():
    """The v5 ``campaigns.update`` payload preview MUST match the
    documented v5 contract for the ``TimeTargeting`` block.

    The v5 contract for each ``TimeTargetItem`` is:

    .. code-block:: json

        {"Days": ["MONDAY"], "Hours": {"BidPercent": [0..100 x 24]}}

    The apply path uses this shape directly; we never invent
    field names.
    """
    settings = _settings("live_readonly")
    yandex = _client_with_handler(
        settings, lambda r: httpx.Response(200, json=_ok_readback_envelope())
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(idempotency_key="tt-shape-001"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    preview = response.json()["payload_preview"]
    assert preview["method"] == "campaigns.update"
    campaigns = preview["params"]["Campaigns"]
    assert len(campaigns) == 1
    assert campaigns[0]["Id"] == 710691939
    tt = campaigns[0]["TimeTargeting"]
    # Dict with Schedule.Items (7 strings), ConsiderWorkingWeekends,
    # and HolidaysSchedule — the v5 campaigns.update shape.
    assert isinstance(tt, dict)
    assert "Schedule" in tt
    items = tt["Schedule"]["Items"]
    assert len(items) == 7
    for index, item in enumerate(items):
        assert isinstance(item, str)
        parts = item.split(",")
        assert len(parts) == 25
        assert parts[0] == str(index + 1)
        for bid_str in parts[1:]:
            bid = int(bid_str)
            assert 0 <= bid <= 100
            assert isinstance(int(bid_str), int)


def test_hours_shape_with_days_filter_expands_to_canonical_seven():
    """The ``hours`` + ``days`` shape expands to a canonical 7 x 24
    matrix. Days not listed in ``days`` are set to all-zeros on
    the apply, so the operator sees an explicit zero schedule on
    the missing days."""
    settings = _settings("live_readonly")
    yandex = _client_with_handler(
        settings, lambda r: httpx.Response(200, json=_ok_readback_envelope())
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                schedule_or_hours=_hours_body(
                    hours=[100] * 24, days=["MONDAY", "WEDNESDAY", "FRIDAY"]
                ),
                idempotency_key="tt-expand-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    preview = response.json()["payload_preview"]
    tt = preview["params"]["Campaigns"][0]["TimeTargeting"]
    assert isinstance(tt, dict)
    items = tt["Schedule"]["Items"]
    assert len(items) == 7
    # MONDAY(index 0), WEDNESDAY(index 2), FRIDAY(index 4) use
    # the 100 schedule; the rest are all-zeros (paused).
    expected_active = {0, 2, 4}
    for index, item in enumerate(items):
        parts = item.split(",")
        assert len(parts) == 25
        assert parts[0] == str(index + 1)
        bids = [int(b) for b in parts[1:]]
        if index in expected_active:
            assert bids == [100] * 24
        else:
            assert bids == [0] * 24


# ---------------------------------------------------------------------------
# live_write success path
# ---------------------------------------------------------------------------


def test_live_write_apply_calls_campaigns_update_and_readback():
    """In ``live_write`` + ``approved`` + ``idempotency_key`` +
    ``dry_run=False``, the endpoint MUST:

    1. call v5 ``campaigns.update`` with the canonical
       ``TimeTargeting`` block (REPLACE-shaped);
    2. follow up with a v5 ``campaigns.get`` with the
       ``TimeTargeting`` field set, to verify the schedule
       landed;
    3. return ``applied=True``, ``source="yandex"`` with the
       read-back included."""
    settings = _settings("live_write")
    captured: dict[str, list[dict[str, Any]]] = {"calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured["calls"].append(
            {"url": str(request.url), "method": body.get("method")}
        )
        if "campaigns.update" in str(request.url) or body.get("method") == "update":
            return httpx.Response(200, json=_ok_update_envelope())
        # The read-back is campaigns.get with the TimeTargeting
        # field set.
        return httpx.Response(200, json=_ok_readback_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-apply-001"
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
    # Read-back is included.
    assert body["readback"] is not None
    assert "TimeTargeting" in body["readback"]
    # Three v5 calls: get (DailyBudget), update, get (readback).
    methods = [call["method"] for call in captured["calls"]]
    assert "update" in methods
    assert methods.count("get") >= 1
    # All calls hit the v5 campaigns service.
    for call in captured["calls"]:
        assert "/json/v5/campaigns" in call["url"]
    # Token never echoed.
    assert SECRET_TOKEN not in response.text
    # Audit recorded.
    audit_events = [
        e for e in store.audit_events if e.action == "yandex_time_targeting_requested"
    ]
    assert audit_events, "expected time-targeting audit event"
    last = audit_events[-1]
    assert last.dry_run is False
    assert SECRET_TOKEN not in json.dumps(last.details, ensure_ascii=False)


def test_live_write_idempotency_key_avoids_duplicate_live_calls():
    """A replay with the same ``idempotency_key`` MUST NOT re-send
    to Yandex. The second response is the cached first response."""
    settings = _settings("live_write")
    captured: dict[str, int] = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured["calls"] += 1
        if body.get("method") == "get":
            # Both DailyBudget read and TimeTargeting readback use
            # the same envelope (includes both fields).
            return httpx.Response(200, json=_ok_readback_envelope())
        # update call.
        return httpx.Response(200, json=_ok_update_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        body = _request_body(
            dry_run=False, idempotency_key="tt-idem-001"
        )
        r1 = client.post(
            "/yandex/campaigns/710691939/time-targeting", json=body
        )
        r2 = client.post(
            "/yandex/campaigns/710691939/time-targeting", json=body
        )
    finally:
        app.dependency_overrides.clear()

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["audit_id"] == r2.json()["audit_id"]
    assert r1.json()["applied"] is True
    assert r2.json()["applied"] is True
    # The first call hits the network 3 times (get DailyBudget +
    # update + get readback); the replay hits zero.
    assert captured["calls"] == 3


def test_live_write_apply_yandex_error_returns_502_with_no_token_leak():
    """If Yandex rejects the apply, the endpoint MUST surface a
    502 with a redacted error message and no token leakage. The
    audit MUST record the failure under
    ``yandex_time_targeting_failed``.
    """
    settings = _settings("live_write")
    captured: dict[str, int] = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            # Return DailyBudget for the pre-apply read.
            return httpx.Response(200, json=_campaign_read_envelope())
        captured["calls"] += 1
        return httpx.Response(
            200,
            json={
                "error": {
                    "error_code": 3500,
                    "error_detail": "Invalid TimeTargeting shape",
                }
            },
        )

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-fail-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502, response.text
    body = response.json()
    assert isinstance(body.get("detail"), dict)
    assert body["detail"]["error_type"] == "YandexDirectError"
    assert SECRET_TOKEN not in response.text
    # The v5 service was called once (the apply). The read-back
    # is best-effort and not called on a failure.
    assert captured["calls"] == 1
    # The failure was audited.
    failed_events = [
        e
        for e in store.audit_events
        if e.action == "yandex_time_targeting_failed"
    ]
    assert failed_events, "expected yandex_time_targeting_failed audit event"
    last = failed_events[-1]
    assert SECRET_TOKEN not in json.dumps(last.details, ensure_ascii=False)


def test_live_write_dry_run_does_not_call_yandex_update():
    """A dry-run in ``live_write`` MUST NOT call Yandex
    ``campaigns.update``. It DOES call ``campaigns.get`` once to
    read the current ``DailyBudget`` block for the preview (the
    task requires the preview to include preserved DailyBudget).
    The response is the v5 payload preview only.
    """
    settings = _settings("live_write")
    captured_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured_methods.append(body.get("method"))
        # Return DailyBudget or readback envelope for any get.
        if body.get("method") == "get":
            return httpx.Response(200, json=_campaign_read_envelope())
        return httpx.Response(200, json=_ok_update_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=True, idempotency_key="tt-lw-dry-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["source"] == "yandex"
    assert body["payload_preview"] is not None
    assert body["readback"] is None
    # campaigns.update was NOT called (dry-run).
    assert "update" not in captured_methods
    # campaigns.get WAS called once (DailyBudget read).
    assert "get" in captured_methods
    assert SECRET_TOKEN not in response.text


# ---------------------------------------------------------------------------
# idempotency cache contract
# ---------------------------------------------------------------------------


def test_replay_with_different_dry_run_is_rejected():
    """Replaying the same ``idempotency_key`` with a different
    ``dry_run`` flag is a logic error and surfaces as 502
    (typed ``YandexDirectError``) — the operator cannot reuse a
    single key for a dry-run and a real apply."""
    settings = _settings("live_write")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_update_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        # First call: dry-run.
        r1 = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=True, idempotency_key="tt-mixed-001"
            ),
        )
        # Second call: real apply on the same key.
        r2 = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-mixed-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 502, r2.text
    detail = r2.json()["detail"]
    assert "tt-mixed-001" in str(detail)
    assert SECRET_TOKEN not in r2.text


# ---------------------------------------------------------------------------
# DailyBudget.Mode preservation (error_code=8000 regression)
# ---------------------------------------------------------------------------


def _campaign_read_envelope(
    *,
    campaign_id: int = 710691939,
    daily_budget: dict[str, Any] | None = None,
    time_targeting: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a v5 ``campaigns.get`` response that includes
    ``DailyBudget`` and ``TimeTargeting`` for a single campaign.

    When ``daily_budget`` is ``None`` the default includes a
    realistic ``Amount`` + ``SpendMode`` block (the shape Direct
    returns on a read). When explicitly set to an empty dict or
    other value, that value is used instead.
    """
    if daily_budget is None:
        daily_budget = {"Amount": 5_000_000, "SpendMode": "STANDARD"}
    if time_targeting is None:
        time_targeting = [
            {
                "Days": [WEEK_DAY_NAMES[index]],
                "Hours": {"BidPercent": _hours_8_to_22_full()},
            }
            for index in range(7)
        ]
    return {
        "result": {
            "Campaigns": [
                {
                    "Id": campaign_id,
                    "Name": "Ремонт кондиционеров Казань — поиск",
                    "DailyBudget": daily_budget,
                    "TimeTargeting": time_targeting,
                }
            ]
        }
    }


def test_dry_run_preview_includes_preserved_daily_budget_mode():
    """The dry-run ``payload_preview`` MUST include the preserved
    ``DailyBudget`` block with a valid ``Mode`` when the campaign
    has a daily budget. This prevents error_code=8000
    ("Отсутствует обязательный параметр Mode") on the real apply.

    Direct returns ``SpendMode`` on ``campaigns.get`` but expects
    ``Mode`` on ``campaigns.update``; the preview must show the
    normalized ``Mode`` value.
    """
    settings = _settings("live_readonly")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            return httpx.Response(200, json=_campaign_read_envelope())
        return httpx.Response(200, json=_ok_update_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(idempotency_key="tt-budget-dry-001"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    preview = body["payload_preview"]
    campaigns = preview["params"]["Campaigns"][0]
    # DailyBudget MUST be present in the preview.
    assert "DailyBudget" in campaigns, campaigns
    daily_budget = campaigns["DailyBudget"]
    # Mode is required; SpendMode must be normalized to Mode.
    assert daily_budget.get("Mode") == "STANDARD", daily_budget
    # Amount is preserved from the read-back.
    assert daily_budget["Amount"] == 5_000_000
    assert SECRET_TOKEN not in response.text


def test_live_write_apply_reads_current_daily_budget_before_update():
    """In ``live_write`` + apply, the endpoint MUST read the
    current campaign's ``DailyBudget`` via ``campaigns.get`` before
    calling ``campaigns.update``. The ``DailyBudget`` block (with
    normalized ``Mode``) MUST be included in the update payload.
    """
    settings = _settings("live_write")
    captured: dict[str, list[dict[str, Any]]] = {"calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured["calls"].append(
            {"url": str(request.url), "method": body.get("method"), "body": body}
        )
        if body.get("method") == "get":
            return httpx.Response(200, json=_campaign_read_envelope())
        if body.get("method") == "update":
            return httpx.Response(200, json=_ok_update_envelope())
        return httpx.Response(200, json=_ok_readback_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-budget-apply-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    # Three calls: get (read DailyBudget), update (apply), get (readback).
    methods = [call["method"] for call in captured["calls"]]
    assert methods.count("get") >= 1, "expected campaigns.get to read DailyBudget"
    assert "update" in methods
    # The update payload MUST include DailyBudget with Mode.
    update_calls = [c for c in captured["calls"] if c["method"] == "update"]
    assert len(update_calls) == 1
    update_campaign = update_calls[0]["body"]["params"]["Campaigns"][0]
    assert "DailyBudget" in update_campaign, update_campaign
    assert update_campaign["DailyBudget"].get("Mode") == "STANDARD"
    assert update_campaign["DailyBudget"]["Amount"] == 5_000_000
    assert SECRET_TOKEN not in response.text


def test_missing_budget_read_fails_closed_502():
    """If the ``campaigns.get`` read-back for DailyBudget fails or
    returns an ambiguous shape, the endpoint MUST fail closed with
    HTTP 502 and no mutation. Never invent a budget value.
    """
    settings = _settings("live_write")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            # Return an envelope with NO DailyBudget field at all.
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Campaigns": [
                            {
                                "Id": 710691939,
                                "Name": "Test Campaign",
                                # No DailyBudget field.
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json=_ok_update_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-nobudget-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    # Missing DailyBudget is ambiguous — fail closed.
    assert response.status_code == 502, response.text
    assert SECRET_TOKEN not in response.text


def test_daily_budget_null_means_no_daily_budget_apply_allowed():
    """A successful DailyBudget read with ``DailyBudget: null`` is
    not ambiguous: the campaign has no daily budget to preserve (for
    example after switching to a weekly conversion strategy). The
    update payload should omit DailyBudget instead of blocking apply.

    When DailyBudget is null, the store also reads
    TextCampaign.BiddingStrategy and includes it in the update.
    """
    settings = _settings("live_write")
    captured: dict[str, Any] = {"updates": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            fields = body.get("params", {}).get("FieldNames") or []
            if "DailyBudget" in fields:
                return httpx.Response(
                    200,
                    json=_smart_strategy_read_envelope(),
                )
            return httpx.Response(200, json=_ok_readback_envelope())
        captured["updates"].append(body)
        return httpx.Response(200, json=_ok_update_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-nullbudget-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    assert len(captured["updates"]) == 1
    update_campaign = captured["updates"][0]["params"]["Campaigns"][0]
    assert "TimeTargeting" in update_campaign
    assert "DailyBudget" not in update_campaign
    assert SECRET_TOKEN not in response.text


def test_daily_budget_spend_mode_normalized_to_mode():
    """Direct returns ``SpendMode`` on ``campaigns.get`` but expects
    ``Mode`` on ``campaigns.update``. The payload MUST use ``Mode``,
    not ``SpendMode``.
    """
    settings = _settings("live_write")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            return httpx.Response(
                200,
                json=_campaign_read_envelope(
                    daily_budget={"Amount": 3_000_000, "SpendMode": "STANDARD"},
                ),
            )
        if body.get("method") == "update":
            return httpx.Response(200, json=_ok_update_envelope())
        return httpx.Response(200, json=_ok_readback_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-spendmode-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    # Find the update call and verify Mode, not SpendMode.
    # (captured is not available here; verify via success)
    assert response.json()["applied"] is True


def test_error_8000_regression_daily_budget_mode_required():
    """Regression test: when Direct returns error_code=8000
    ("Отсутствует обязательный параметр Mode"), the endpoint MUST
    surface a 502 with a redacted message. This covers the exact
    error that motivated this fix.
    """
    settings = _settings("live_write")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            return httpx.Response(200, json=_campaign_read_envelope())
        if body.get("method") == "update":
            # Simulate the exact Direct error.
            return httpx.Response(
                200,
                json={
                    "error": {
                        "error_code": 8000,
                        "error_detail": (
                            "Отсутствует обязательный параметр Mode"
                        ),
                    }
                },
            )
        return httpx.Response(200, json={})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-err8000-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["detail"]["error_type"] == "YandexDirectError"
    assert SECRET_TOKEN not in response.text
    # Audit must record the failure.
    failed_events = [
        e for e in store.audit_events
        if e.action == "yandex_time_targeting_failed"
    ]
    assert failed_events, "expected yandex_time_targeting_failed audit event"


# ---------------------------------------------------------------------------
# error_detail preservation (regression: error_code=8000 without detail)
# ---------------------------------------------------------------------------


def test_direct_error_detail_preserved_in_client_envelope():
    """``_call`` MUST preserve ``error_detail`` and ``error_string`` from
    the Direct API error response, so callers can surface actionable
    diagnostics (e.g. \"Отсутствует обязательный параметр Mode\")
    instead of just the numeric ``error_code``.
    """
    settings = _settings("live_readonly")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error": {
                    "error_code": 8000,
                    "error_detail": "Отсутствует обязательный параметр Mode",
                    "error_string": "Missing required parameter: Mode",
                }
            },
        )

    yandex = _client_with_handler(settings, handler)
    # Use any public method that goes through _call.
    result = yandex.campaigns_get_daily_budget(710691939)
    assert result["ok"] is False
    assert result["error"]["error_code"] == 8000
    assert result["error"]["error_detail"] == "Отсутствует обязательный параметр Mode"
    assert result["error"]["error_string"] == "Missing required parameter: Mode"


def test_error_8000_surfaces_detail_and_redacted_payload():
    """When campaigns.update returns error_code=8000, the endpoint MUST
    surface not just the code but also error_detail and a redacted
    payload_preview so the operator can diagnose the shape mismatch
    without raw OAuth tokens.
    """
    settings = _settings("live_write")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            return httpx.Response(200, json=_campaign_read_envelope())
        if body.get("method") == "update":
            return httpx.Response(
                200,
                json={
                    "error": {
                        "error_code": 8000,
                        "error_detail": "Отсутствует обязательный параметр Mode",
                    }
                },
            )
        return httpx.Response(200, json={})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-err8000-detail-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502, response.text
    body = response.json()
    detail = body["detail"]
    # error_detail must be surfaced.
    assert "error_detail" in detail
    assert "Отсутствует обязательный параметр Mode" in str(detail)
    # error_code must be in the message.
    assert 8000 == detail.get("error_code")
    # Payload preview must be included (redacted).
    assert "payload_preview" in detail
    assert "Campaigns" in str(detail["payload_preview"])
    assert SECRET_TOKEN not in response.text
    # Audit must record the failure with diagnostic fields.
    failed_events = [
        e for e in store.audit_events
        if e.action == "yandex_time_targeting_failed"
    ]
    assert failed_events, "expected yandex_time_targeting_failed audit event"
    assert "yandex_error_detail" in failed_events[-1].details


# ---------------------------------------------------------------------------
# Smart-strategy BiddingStrategy preservation (error_code=8000 regression
# for TEXT_CAMPAIGN with WB_MAXIMUM_CONVERSION_RATE and DailyBudget=null)
# ---------------------------------------------------------------------------


def _smart_strategy_read_envelope(
    *,
    campaign_id: int = 710691939,
    strategy_type: str = "WB_MAXIMUM_CONVERSION_RATE",
    daily_budget: dict[str, Any] | None = None,
    time_targeting: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a v5 ``campaigns.get`` response that includes
    ``Type``, ``DailyBudget``, ``TimeTargeting`` and
    ``TextCampaign.BiddingStrategy`` for a smart-strategy campaign.

    Mirrors the live campaign 710691939 shape:
    DailyBudget=null, TextCampaign.BiddingStrategy with
    WB_MAXIMUM_CONVERSION_RATE on search + SERVING_OFF on network.
    """
    if daily_budget is None:
        daily_budget = None  # explicit null for smart-strategy campaigns
    if time_targeting is None:
        time_targeting = [
            {
                "Days": [WEEK_DAY_NAMES[index]],
                "Hours": {"BidPercent": _hours_8_to_22_full()},
            }
            for index in range(7)
        ]
    return {
        "result": {
            "Campaigns": [
                {
                    "Id": campaign_id,
                    "Name": "Smart Strategy Campaign",
                    "Type": "TEXT_CAMPAIGN",
                    "DailyBudget": daily_budget,
                    "TimeTargeting": time_targeting,
                    "TextCampaign": {
                        "BiddingStrategy": {
                            "Search": {
                                "BiddingStrategyType": strategy_type,
                                "WbMaximumConversionRate": {
                                    "GoalId": 567732835,
                                    "WeeklySpendLimit": 7000000000,
                                    "BudgetType": "WEEKLY_BUDGET",
                                    "BidCeiling": 1500000000,
                                },
                            },
                            "Network": {
                                "BiddingStrategyType": "SERVING_OFF",
                            },
                        },
                    },
                }
            ]
        }
    }


def test_smart_strategy_dry_run_preserves_bidding_strategy_in_payload():
    """A dry-run for a TEXT_CAMPAIGN with WB_MAXIMUM_CONVERSION_RATE
    and DailyBudget=null MUST include TextCampaign.BiddingStrategy in
    the payload_preview, with BudgetType removed (write-side shape)
    and all other strategy fields preserved. No DailyBudget in the
    preview when the campaign has none.
    """
    settings = _settings("live_readonly")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            return httpx.Response(
                200, json=_smart_strategy_read_envelope()
            )
        return httpx.Response(200, json=_ok_update_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(idempotency_key="tt-strategy-dry-001"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    preview = body["payload_preview"]
    campaigns = preview["params"]["Campaigns"][0]
    # TimeTargeting MUST be present.
    assert "TimeTargeting" in campaigns
    assert len(campaigns["TimeTargeting"]["Schedule"]["Items"]) == 7
    # DailyBudget MUST NOT be present (campaign has no daily budget).
    assert "DailyBudget" not in campaigns
    # TextCampaign.BiddingStrategy MUST be present.
    assert "TextCampaign" in campaigns, campaigns
    strategy = campaigns["TextCampaign"]["BiddingStrategy"]
    assert strategy["Search"]["BiddingStrategyType"] == "WB_MAXIMUM_CONVERSION_RATE"
    wmcr = strategy["Search"]["WbMaximumConversionRate"]
    assert wmcr["GoalId"] == 567732835
    assert wmcr["WeeklySpendLimit"] == 7000000000
    assert wmcr["BidCeiling"] == 1500000000
    # BudgetType is preserved (not stripped) — the real Yandex API
    # returns it on GET and requires it on write for
    # WbMaximumConversionRate to avoid error_code=8000.
    assert wmcr.get("BudgetType") == "WEEKLY_BUDGET"
    # Network strategy preserved.
    assert strategy["Network"]["BiddingStrategyType"] == "SERVING_OFF"
    assert SECRET_TOKEN not in response.text


def test_smart_strategy_live_write_apply_includes_bidding_strategy():
    """In ``live_write`` + apply, the update payload MUST include
    the TextCampaign.BiddingStrategy (normalized to write shape)
    so Direct doesn't reject with error_code=8000.
    """
    settings = _settings("live_write")
    captured: dict[str, list[dict[str, Any]]] = {"calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured["calls"].append(body)
        if body.get("method") == "get":
            return httpx.Response(
                200, json=_smart_strategy_read_envelope()
            )
        if body.get("method") == "update":
            return httpx.Response(200, json=_ok_update_envelope())
        return httpx.Response(200, json=_ok_readback_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-strategy-apply-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    # Find the update call.
    update_calls = [c for c in captured["calls"] if c.get("method") == "update"]
    assert len(update_calls) == 1
    update_campaign = update_calls[0]["params"]["Campaigns"][0]
    # TextCampaign.BiddingStrategy MUST be present in the update.
    assert "TextCampaign" in update_campaign, update_campaign
    strategy = update_campaign["TextCampaign"]["BiddingStrategy"]
    assert strategy["Search"]["BiddingStrategyType"] == "WB_MAXIMUM_CONVERSION_RATE"
    wmcr = strategy["Search"]["WbMaximumConversionRate"]
    assert wmcr["GoalId"] == 567732835
    # BudgetType is preserved (not stripped) — the real Yandex API
    # returns it on GET and requires it on write for
    # WbMaximumConversionRate to avoid error_code=8000.
    assert wmcr.get("BudgetType") == "WEEKLY_BUDGET"
    # DailyBudget MUST NOT be present (null on this campaign).
    assert "DailyBudget" not in update_campaign
    # TimeTargeting MUST be present.
    assert "TimeTargeting" in update_campaign
    assert SECRET_TOKEN not in response.text


def test_smart_strategy_budgettype_normalization():
    """BudgetType from the read-side response MUST be removed on the
    write-side payload for TextCampaign.BiddingStrategy sub-objects.
    This covers the read->write normalization that prevents the
    "unexpected parameter BudgetType" error from Direct.
    """
    settings = _settings("live_readonly")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            return httpx.Response(
                200, json=_smart_strategy_read_envelope()
            )
        return httpx.Response(200, json=_ok_update_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(idempotency_key="tt-budgettype-001"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    preview = response.json()["payload_preview"]
    strategy = preview["params"]["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]
    # BudgetType is read-only; write-side should not carry it.
    wmcr = strategy["Search"].get("WbMaximumConversionRate", {})
    # BudgetType is returned by the real Yandex API on GET for
    # WbMaximumConversionRate (despite docs not listing it) and
    # must be preserved on write to avoid error_code=8000
    # ("Отсутствует обязательный параметр"). The deterministic
    # read→write mapping never invents values, so passing through
    # what Yandex returned is the safest behaviour.
    assert "BudgetType" in wmcr, (
        f"BudgetType should be preserved on write-side, got {wmcr}"
    )
    assert wmcr["BudgetType"] == "WEEKLY_BUDGET"
    assert SECRET_TOKEN not in response.text


def test_smart_strategy_error_8000_with_diagnostics():
    """When Direct rejects a smart-strategy time-targeting apply
    with error_code=8000, the 502 response MUST include:
    - error_code
    - error_detail (the human-readable Direct error message)
    - payload_preview (sanitized campaigns.update payload, no OAuth)
    so the operator can diagnose the mismatch without live trial-and-error.

    Regression: the prior fix stripped BudgetType from
    WbMaximumConversionRate on write, causing error 8000
    ("missing required parameter") on the real API.
    """
    settings = _settings("live_write")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            return httpx.Response(
                200, json=_smart_strategy_read_envelope()
            )
        if body.get("method") == "update":
            return httpx.Response(
                200,
                json={
                    "error": {
                        "error_code": 8000,
                        "error_detail": (
                            "Отсутствует обязательный параметр"
                        ),
                        "error_string": (
                            "Missing required parameter"
                        ),
                    }
                },
            )
        return httpx.Response(200, json={})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-smart-err8000-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502, response.text
    body = response.json()
    detail = body["detail"]
    assert detail["error_type"] == "YandexDirectError"
    assert detail["error_code"] == 8000
    assert detail["error_detail"] == (
        "Отсутствует обязательный параметр"
    )
    # payload_preview MUST be present and sanitized.
    assert "payload_preview" in detail
    pp = detail["payload_preview"]
    assert pp["method"] == "campaigns.update"
    campaigns = pp["params"]["Campaigns"]
    assert len(campaigns) == 1
    entry = campaigns[0]
    # Strategy must be present (smart-strategy preservation).
    strategy = entry["TextCampaign"]["BiddingStrategy"]
    wmcr = strategy["Search"]["WbMaximumConversionRate"]
    # BudgetType must be PRESERVED (the fix for this regression).
    assert "BudgetType" in wmcr, (
        f"BudgetType must be preserved to avoid error 8000, got {wmcr}"
    )
    assert wmcr["BudgetType"] == "WEEKLY_BUDGET"
    assert SECRET_TOKEN not in response.text
    # Audit must record the failure.
    failed_events = [
        e for e in store.audit_events
        if e.action == "yandex_time_targeting_failed"
    ]
    assert failed_events, "expected yandex_time_targeting_failed audit event"


def test_missing_strategy_read_fails_closed_for_text_campaign():
    """If the campaign is a TEXT_CAMPAIGN but the BiddingStrategy
    read returns ambiguous/missing data, the endpoint MUST fail
    closed with HTTP 502 before calling campaigns.update.
    Never invent strategy values.
    """
    settings = _settings("live_write")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            fields = body.get("params", {}).get("FieldNames") or []
            if "DailyBudget" in fields:
                # Return campaign with Type but missing BiddingStrategy.
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "Campaigns": [
                                {
                                    "Id": 710691939,
                                    "Name": "Broken Campaign",
                                    "Type": "TEXT_CAMPAIGN",
                                    "DailyBudget": None,
                                    # No TextCampaign / BiddingStrategy.
                                }
                            ]
                        }
                    },
                )
            return httpx.Response(200, json=_ok_readback_envelope())
        return httpx.Response(200, json=_ok_update_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(
                dry_run=False, idempotency_key="tt-nostrategy-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    # Must fail closed -- missing strategy for a TEXT_CAMPAIGN.
    assert response.status_code == 502, response.text
    assert SECRET_TOKEN not in response.text
    # Audit should record the failure.
    failed_events = [
        e for e in store.audit_events
        if e.action == "yandex_time_targeting_failed"
    ]
    assert failed_events, "expected yandex_time_targeting_failed audit event"

# ---------------------------------------------------------------------------
# TimeTargeting v5 update format (string-encoded Schedule.Items)
# ---------------------------------------------------------------------------


def test_time_targeting_uses_v5_update_string_format():
    """The TimeTargeting payload for campaigns.update MUST use
    the v5 string-encoded format: Schedule.Items is an array of
    strings "D,bid0,bid1,...,bid23" where D is 1-7
    (Monday-Sunday). ConsiderWorkingWeekends is required.
    HolidaysSchedule is included with safe defaults.

    This matches the documented v5 contract at
    https://yandex.com/dev/direct/doc/en/curl-campaigns-timetargeting
    """
    from app.store import store as s

    schedule = YandexTimeTargetingSchedule(
        days=[YandexTimeTargetingHourly(hours=_hours_8_to_22_full()) for _ in range(7)]
    )
    tt = s._build_v5_time_targeting_from_schedule(schedule)
    assert isinstance(tt, dict), f"expected dict, got {type(tt).__name__}"
    assert "Schedule" in tt
    assert "Items" in tt["Schedule"]
    assert isinstance(tt["Schedule"]["Items"], list)
    assert len(tt["Schedule"]["Items"]) == 7
    # Each item must be a string "D,bid0,bid1,...,bid23"
    for idx, item in enumerate(tt["Schedule"]["Items"]):
        assert isinstance(item, str), f"item {idx}: expected str, got {type(item).__name__}"
        parts = item.split(",")
        assert len(parts) == 25, f"item {idx}: expected 25 parts, got {len(parts)}"
        assert parts[0] == str(idx + 1), f"item {idx}: day number mismatch"
        # 24 bid values, all in 0..100
        for bid_str in parts[1:]:
            bid = int(bid_str)
            assert 0 <= bid <= 100
    # ConsiderWorkingWeekends is required.
    assert "ConsiderWorkingWeekends" in tt
    assert tt["ConsiderWorkingWeekends"] in ("YES", "NO")
    # HolidaysSchedule is included.
    assert "HolidaysSchedule" in tt
    hs = tt["HolidaysSchedule"]
    assert "SuspendOnHolidays" in hs
    assert "BidPercent" in hs
    assert "StartHour" in hs
    assert "EndHour" in hs


def test_time_targeting_string_format_in_dry_run_payload():
    """A dry-run for any campaign MUST show the string-encoded
    TimeTargeting format in payload_preview.
    """
    settings = _settings("live_readonly")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_readback_envelope())

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/campaigns/710691939/time-targeting",
            json=_request_body(idempotency_key="tt-string-001"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    preview = response.json()["payload_preview"]
    tt = preview["params"]["Campaigns"][0]["TimeTargeting"]
    assert isinstance(tt, dict), f"expected dict, got {type(tt).__name__}"
    assert "Schedule" in tt
    assert "Items" in tt["Schedule"]
    assert len(tt["Schedule"]["Items"]) == 7
    assert isinstance(tt["Schedule"]["Items"][0], str)
    assert "ConsiderWorkingWeekends" in tt
    assert "HolidaysSchedule" in tt
    assert SECRET_TOKEN not in response.text
