"""Characterize process-local audit and idempotency behavior only."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings


_CREDENTIAL_FIELD_NAMES = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "oauth_token",
    "password",
    "secret",
    "token",
}


def _contains_credential_like_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _CREDENTIAL_FIELD_NAMES
            or _contains_credential_like_field(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_credential_like_field(item) for item in value)
    return False


def test_audit_event_shape_and_process_local_idempotency_boundary(
    client: TestClient,
    yandex_settings: Callable[..., Settings],
    override_dependencies: Callable[..., None],
    exploding_provider: object,
) -> None:
    """This records only same-process in-memory replay behavior, not durability or tenancy."""

    override_dependencies(yandex_settings("mock"), exploding_provider)
    payload = {
        "approved": True,
        "idempotency_key": "characterization-idempotency-001",
        "dry_run": True,
    }

    first = client.post("/yandex/campaigns/characterization-idempotency/pause", json=payload)
    repeat = client.post("/yandex/campaigns/characterization-idempotency/pause", json=payload)
    conflicting_payload = {**payload, "dry_run": False}
    conflicting = client.post(
        "/yandex/campaigns/characterization-idempotency/pause",
        json=conflicting_payload,
    )

    assert first.status_code == 200
    assert repeat.status_code == 200
    assert first.json() == repeat.json()
    # The current in-memory cache replays the first result for this conflict.
    assert conflicting.status_code == 200
    assert conflicting.json() == first.json()
    assert exploding_provider.network_attempts == []  # type: ignore[attr-defined]

    audit_id = first.json()["audit_id"]
    audit_events = client.get("/audit-log").json()["items"]
    matching_events = [event for event in audit_events if event["id"] == audit_id]

    assert len(matching_events) == 1
    event = matching_events[0]
    assert {"id", "actor", "action", "entity", "dry_run", "details"}.issubset(event)
    assert event["action"] == "yandex_pause_requested"
    assert event["entity"] == "characterization-idempotency"
    assert _contains_credential_like_field(event) is False
