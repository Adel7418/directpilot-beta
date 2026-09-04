from __future__ import annotations

from app.modules.tenancy.models import MembershipRole


def test_membership_roles_are_limited_to_the_p3_policy_roles() -> None:
    assert {role.value for role in MembershipRole} == {
        "viewer",
        "operator",
        "approver",
        "admin",
        "owner",
    }
