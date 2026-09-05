from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import MembershipRecord, UserRecord, WorkspaceRecord
from app.db.rls import TenantContext, set_local_tenant_context, tenant_transaction
from app.modules.tenancy.models import (
    MembershipRole,
    MembershipStatus,
    UserStatus,
    WorkspaceKind,
    WorkspaceStatus,
)


@dataclass(frozen=True, slots=True)
class PersonalWorkspace:
    """The three records that must succeed or fail in one transaction."""

    user: UserRecord
    workspace: WorkspaceRecord
    membership: MembershipRecord


class PostgresIdentityRepository:
    """Persist P3 identity records without provider/OAuth behavior."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create_personal_workspace(
        self,
        *,
        display_name: str,
        workspace_name: str,
        yandex_subject: str | None = None,
        email_normalized: str | None = None,
    ) -> PersonalWorkspace:
        if not display_name.strip() or not workspace_name.strip():
            raise ValueError("display name and workspace name are required")

        now = datetime.now(timezone.utc)
        user = UserRecord(
            id=uuid4(),
            yandex_subject=yandex_subject,
            email_normalized=email_normalized,
            display_name=display_name.strip(),
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            last_login_at=now,
        )
        workspace = WorkspaceRecord(
            id=uuid4(),
            name=workspace_name.strip(),
            kind=WorkspaceKind.PERSONAL,
            status=WorkspaceStatus.ACTIVE,
            created_by_user_id=user.id,
            created_at=now,
            updated_at=now,
        )
        membership = MembershipRecord(
            id=uuid4(),
            workspace_id=workspace.id,
            user_id=user.id,
            role=MembershipRole.OWNER,
            status=MembershipStatus.ACTIVE,
            created_at=now,
            accepted_at=now,
            revoked_at=None,
        )

        with self._sessions() as session:
            with session.begin():
                session.add(user)
                session.flush()
                set_local_tenant_context(
                    session,
                    TenantContext(workspace_id=workspace.id, user_id=user.id),
                )
                session.add(workspace)
                session.flush()
                session.add(membership)
                session.flush()

        return PersonalWorkspace(user=user, workspace=workspace, membership=membership)

    def update_membership(
        self,
        *,
        workspace_id: UUID | str,
        user_id: UUID | str,
        role: MembershipRole,
        status: MembershipStatus,
    ) -> None:
        """Persist role/revocation changes that future authorizers re-read."""

        now = datetime.now(timezone.utc)
        with tenant_transaction(
            self._sessions,
            workspace_id=workspace_id,
            user_id=user_id,
        ) as session:
            record = session.scalar(
                select(MembershipRecord)
                .where(
                    MembershipRecord.workspace_id == workspace_id,
                    MembershipRecord.user_id == user_id,
                )
                .with_for_update()
            )
            if record is None:
                raise KeyError("workspace membership is unavailable")
            record.role = role
            record.status = status
            record.revoked_at = now if status is MembershipStatus.REVOKED else None
            if status is MembershipStatus.ACTIVE and record.accepted_at is None:
                record.accepted_at = now
