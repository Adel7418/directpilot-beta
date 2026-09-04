from __future__ import annotations

from datetime import datetime, timezone

from starlette.responses import Response

SESSION_COOKIE_NAME = "__Host-directpilot_session"


def set_session_cookie(response: Response, *, token: str, expires_at: datetime) -> None:
    """Set the non-negotiable host-only opaque-session cookie attributes."""

    now = datetime.now(timezone.utc)
    max_age = max(0, int((expires_at - now).total_seconds()))
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        path="/",
        domain=None,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        domain=None,
        secure=True,
        httponly=True,
        samesite="lax",
    )
