"""Characterize preview behavior and the live_readonly write gate."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.config import Settings


def test_live_readonly_dry_run_remains_provider_side_effect_free(
    client: TestClient,
    yandex_settings: Callable[..., Settings],
    override_dependencies: Callable[..., None],
    exploding_provider: object,
) -> None:
    override_dependencies(yandex_settings("live_readonly"), exploding_provider)

    response = client.post(
        "/yandex/campaigns/characterization-preview/pause",
        json={
            "approved": True,
            "idempotency_key": "characterization-preview-001",
            "dry_run": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    assert response.json()["applied"] is False
    assert exploding_provider.network_attempts == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("operation", ("pause", "resume"))
def test_live_readonly_representative_writes_fail_before_provider_io(
    operation: str,
    client: TestClient,
    yandex_settings: Callable[..., Settings],
    override_dependencies: Callable[..., None],
    exploding_provider: object,
) -> None:
    override_dependencies(yandex_settings("live_readonly"), exploding_provider)

    response = client.post(
        f"/yandex/campaigns/characterization-write-gate/{operation}",
        json={
            "approved": True,
            "idempotency_key": f"characterization-{operation}-001",
            "dry_run": False,
        },
    )

    assert response.status_code == 409
    assert exploding_provider.network_attempts == []  # type: ignore[attr-defined]
