from __future__ import annotations

from enum import Enum


class UserStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class WorkspaceKind(str, Enum):
    PERSONAL = "personal"
    TEAM = "team"


class WorkspaceStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class MembershipRole(str, Enum):
    """Roles accepted for an active workspace membership."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    APPROVER = "approver"
    ADMIN = "admin"
    OWNER = "owner"


class MembershipStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


def parse_membership_role(value: str) -> MembershipRole:
    """Validate external role input against the closed P3 role set."""

    try:
        return MembershipRole(value)
    except ValueError as exc:
        raise ValueError("membership role is invalid") from exc
