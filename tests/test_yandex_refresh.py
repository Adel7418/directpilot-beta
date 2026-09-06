from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.modules.integrations.yandex.refresh import (
    DEFAULT_REFRESH_SKEW_SECONDS,
    is_refresh_due,
)


def test_refresh_is_due_at_the_exact_default_skew_boundary() -> None:
    expires_at = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)

    assert DEFAULT_REFRESH_SKEW_SECONDS == 60
    assert not is_refresh_due(
        now=expires_at - timedelta(seconds=DEFAULT_REFRESH_SKEW_SECONDS + 1),
        expires_at=expires_at,
        skew_seconds=DEFAULT_REFRESH_SKEW_SECONDS,
    )
    assert is_refresh_due(
        now=expires_at - timedelta(seconds=DEFAULT_REFRESH_SKEW_SECONDS),
        expires_at=expires_at,
        skew_seconds=DEFAULT_REFRESH_SKEW_SECONDS,
    )
