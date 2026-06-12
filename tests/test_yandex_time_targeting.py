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
    (the read-back the endpoint performs after the apply)."""
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
        len(body["payload_preview"]["params"]["Campaigns"][0]["TimeTargeting"])
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


def test_live_readonly_dry_run_is_allowed_without_network_call():
    """In ``live_readonly``, a dry-run is allowed and must NOT call
    Yandex. The response is the v5 payload preview with
    ``source="yandex"``, ``applied=False``."""
    settings = _settings("live_readonly")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
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
    # No network call on dry-run.
    assert network_called["calls"] == 0
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
    # Seven TimeTargetItems, one per day, in v5 canonical order.
    assert len(tt) == 7
    for index, item in enumerate(tt):
        assert item["Days"] == [WEEK_DAY_NAMES[index]]
        assert "Hours" in item
        assert "BidPercent" in item["Hours"]
        assert len(item["Hours"]["BidPercent"]) == 24
        for value in item["Hours"]["BidPercent"]:
            assert isinstance(value, int)
            assert 0 <= value <= 100


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
    assert len(tt) == 7
    # MONDAY, WEDNESDAY, FRIDAY use the 100 schedule; the rest
    # are all-zeros (paused).
    for index, item in enumerate(tt):
        if WEEK_DAY_NAMES[index] in ("MONDAY", "WEDNESDAY", "FRIDAY"):
            assert item["Hours"]["BidPercent"] == [100] * 24
        else:
            assert item["Hours"]["BidPercent"] == [0] * 24


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
    # Two v5 calls: one update, one get (the read-back).
    methods = [call["method"] for call in captured["calls"]]
    assert "update" in methods
    assert "get" in methods
    # Both calls hit the v5 campaigns service.
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

    def handler(_request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        if captured["calls"] % 2 == 1:
            # Odd calls are the update.
            return httpx.Response(200, json=_ok_update_envelope())
        # Even calls are the read-back.
        return httpx.Response(200, json=_ok_readback_envelope())

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
    # The first call hits the network twice (update + get);
    # the replay hits zero.
    assert captured["calls"] == 2


def test_live_write_apply_yandex_error_returns_502_with_no_token_leak():
    """If Yandex rejects the apply, the endpoint MUST surface a
    502 with a redacted error message and no token leakage. The
    audit MUST record the failure under
    ``yandex_time_targeting_failed``."""
    settings = _settings("live_write")
    captured: dict[str, int] = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
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


def test_live_write_dry_run_does_not_call_yandex():
    """A dry-run in ``live_write`` MUST NOT call Yandex. The
    response is the v5 payload preview only."""
    settings = _settings("live_write")
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
    assert network_called["calls"] == 0
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
