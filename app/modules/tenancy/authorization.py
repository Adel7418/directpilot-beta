from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import MembershipRecord, WorkspaceRecord
from app.db.rls import tenant_transaction
from app.modules.tenancy.models import MembershipRole, MembershipStatus, WorkspaceStatus
from app.modules.tenancy.policy import AuthorizationDenied, Capability, RbacPolicy


@dataclass(frozen=True, slots=True)
class AuthorizedMembership:
    user_id: UUID
    workspace_id: UUID
    role: MembershipRole


class PostgresWorkspaceAuthorizer:
    """Load current membership authority on each operation; sessions hold none."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        policy: RbacPolicy | None = None,
    ) -> None:
        self._sessions = sessions
        self._policy = policy or RbacPolicy()

    def authorize(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        capability: Capability,
    ) -> AuthorizedMembership:
        with tenant_transaction(
            self._sessions,
            workspace_id=workspace_id,
            user_id=user_id,
        ) as session:
            workspace = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.id == workspace_id,
                    WorkspaceRecord.status == WorkspaceStatus.ACTIVE,
                )
            )
            membership = session.scalar(
                select(MembershipRecord).where(
                    MembershipRecord.workspace_id == workspace_id,
                    MembershipRecord.user_id == user_id,
                )
            )
            if workspace is None or membership is None or membership.status is not MembershipStatus.ACTIVE:
                raise AuthorizationDenied("workspace membership is inactive")
            self._policy.require(membership.role, capability)
            return AuthorizedMembership(
                user_id=user_id,
                workspace_id=workspace_id,
                role=membership.role,
            )
