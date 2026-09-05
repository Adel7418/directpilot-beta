from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

LEGACY_OPERATOR_USER_ID = UUID("00000000-0000-4000-8000-000000000301")
LEGACY_OPERATOR_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000302")


class WorkspaceContextError(ValueError):
    """Raised before any tenant query when a workspace context is invalid."""


@dataclass(frozen=True, slots=True)
class TenantContext:
    workspace_id: UUID
    user_id: UUID | None = None


def require_uuid(value: UUID | str, *, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise WorkspaceContextError(f"{field_name} is invalid") from None


def set_local_tenant_context(session: Session, context: TenantContext) -> None:
    """Bind only transaction-local PostgreSQL GUCs for a tenant operation.

    ``set_config(..., true)`` is PostgreSQL's parameter-safe equivalent of
    ``SET LOCAL`` and resets automatically at transaction end.
    """

    session.execute(
        text("SELECT set_config('app.current_workspace_id', :workspace_id, true)"),
        {"workspace_id": str(context.workspace_id)},
    )
    session.execute(
        text("SELECT set_config('app.current_user_id', :user_id, true)"),
        {"user_id": "" if context.user_id is None else str(context.user_id)},
    )


@contextmanager
def tenant_transaction(
    sessions: sessionmaker[Session],
    *,
    workspace_id: UUID | str,
    user_id: UUID | str | None = None,
) -> Generator[Session, None, None]:
    """Yield one transaction that cannot leak tenant GUCs into a pool."""

    context = TenantContext(
        workspace_id=require_uuid(workspace_id, field_name="workspace id"),
        user_id=(None if user_id is None else require_uuid(user_id, field_name="user id")),
    )
    with sessions() as session:
        with session.begin():
            set_local_tenant_context(session, context)
            yield session
