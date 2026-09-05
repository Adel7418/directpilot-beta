from __future__ import annotations

from enum import Enum

from app.modules.tenancy.models import MembershipRole


class Capability(str, Enum):
    READ_WORKSPACE_DATA = "read_workspace_data"
    CREATE_PROPOSAL = "create_proposal"
    APPROVE_PROPOSAL = "approve_proposal"
    EXECUTE_APPROVED_ACTION = "execute_approved_action"
    MANAGE_PROVIDER_CONNECTIONS = "manage_provider_connections"
    MANAGE_MEMBERS = "manage_members"
    DELETE_OR_TRANSFER_WORKSPACE = "delete_or_transfer_workspace"


class AuthorizationDenied(PermissionError):
    """Raised when the current membership does not grant a capability."""


_ALLOWED_ROLES: dict[Capability, frozenset[MembershipRole]] = {
    Capability.READ_WORKSPACE_DATA: frozenset(MembershipRole),
    Capability.CREATE_PROPOSAL: frozenset(
        {
            MembershipRole.OPERATOR,
            MembershipRole.APPROVER,
            MembershipRole.ADMIN,
            MembershipRole.OWNER,
        }
    ),
    Capability.APPROVE_PROPOSAL: frozenset(
        {
            MembershipRole.APPROVER,
            MembershipRole.ADMIN,
            MembershipRole.OWNER,
        }
    ),
    Capability.EXECUTE_APPROVED_ACTION: frozenset(
        {MembershipRole.ADMIN, MembershipRole.OWNER}
    ),
    Capability.MANAGE_PROVIDER_CONNECTIONS: frozenset(
        {MembershipRole.ADMIN, MembershipRole.OWNER}
    ),
    Capability.MANAGE_MEMBERS: frozenset({MembershipRole.ADMIN, MembershipRole.OWNER}),
    Capability.DELETE_OR_TRANSFER_WORKSPACE: frozenset({MembershipRole.OWNER}),
}


class RbacPolicy:
    """The single P3 role-to-capability authority for workspace operations."""

    def allows(self, role: MembershipRole, capability: Capability) -> bool:
        return role in _ALLOWED_ROLES[capability]

    def require(self, role: MembershipRole, capability: Capability) -> None:
        if not self.allows(role, capability):
            raise AuthorizationDenied("workspace access is denied")
