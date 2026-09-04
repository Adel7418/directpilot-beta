from __future__ import annotations

import pytest

from app.modules.tenancy.models import MembershipRole
from app.modules.tenancy.policy import Capability, AuthorizationDenied, RbacPolicy


@pytest.mark.parametrize(
    ("capability", "allowed_roles"),
    [
        (Capability.READ_WORKSPACE_DATA, set(MembershipRole)),
        (
            Capability.CREATE_PROPOSAL,
            {
                MembershipRole.OPERATOR,
                MembershipRole.APPROVER,
                MembershipRole.ADMIN,
                MembershipRole.OWNER,
            },
        ),
        (
            Capability.APPROVE_PROPOSAL,
            {
                MembershipRole.APPROVER,
                MembershipRole.ADMIN,
                MembershipRole.OWNER,
            },
        ),
        (Capability.EXECUTE_APPROVED_ACTION, {MembershipRole.ADMIN, MembershipRole.OWNER}),
        (Capability.MANAGE_PROVIDER_CONNECTIONS, {MembershipRole.ADMIN, MembershipRole.OWNER}),
        (Capability.MANAGE_MEMBERS, {MembershipRole.ADMIN, MembershipRole.OWNER}),
        (Capability.DELETE_OR_TRANSFER_WORKSPACE, {MembershipRole.OWNER}),
    ],
)
def test_rbac_policy_enforces_the_p3_role_capability_matrix(
    capability: Capability,
    allowed_roles: set[MembershipRole],
) -> None:
    policy = RbacPolicy()

    for role in MembershipRole:
        assert policy.allows(role, capability) is (role in allowed_roles)


def test_rbac_policy_raises_a_stable_denial_for_disallowed_capability() -> None:
    with pytest.raises(AuthorizationDenied, match="workspace access is denied"):
        RbacPolicy().require(MembershipRole.VIEWER, Capability.MANAGE_MEMBERS)
