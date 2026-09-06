from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from starlette.responses import Response


@dataclass(frozen=True, slots=True)
class SessionCookiePolicy:
    name: str
    secure: bool
    httponly: bool
    samesite: Literal["lax", "strict", "none"]
    path: str
    domain: str | None


SESSION_COOKIE_POLICY = SessionCookiePolicy(
    name="__Host-directpilot_session",
    secure=True,
    httponly=True,
    samesite="lax",
    path="/",
    domain=None,
)
SESSION_COOKIE_NAME = SESSION_COOKIE_POLICY.name


def set_session_cookie(response: Response, *, token: str, expires_at: datetime) -> None:
    """Set the non-negotiable host-only opaque-session cookie attributes."""

    now = datetime.now(timezone.utc)
    max_age = max(0, int((expires_at - now).total_seconds()))
    response.set_cookie(
        key=SESSION_COOKIE_POLICY.name,
        value=token,
        max_age=max_age,
        path=SESSION_COOKIE_POLICY.path,
        domain=SESSION_COOKIE_POLICY.domain,
        secure=SESSION_COOKIE_POLICY.secure,
        httponly=SESSION_COOKIE_POLICY.httponly,
        samesite=SESSION_COOKIE_POLICY.samesite,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_POLICY.name,
        path=SESSION_COOKIE_POLICY.path,
        domain=SESSION_COOKIE_POLICY.domain,
        secure=SESSION_COOKIE_POLICY.secure,
        httponly=SESSION_COOKIE_POLICY.httponly,
        samesite=SESSION_COOKIE_POLICY.samesite,
    )
