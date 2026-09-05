from __future__ import annotations

import pytest

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.tenancy.authorization import PostgresWorkspaceAuthorizer
from app.modules.tenancy.models import MembershipRole, MembershipStatus
from app.modules.tenancy.policy import AuthorizationDenied, Capability


@pytest.mark.integration
def test_authorization_reloads_role_and_revocation_from_membership_storage(
    postgres_service: object,
) -> None:
    owner_runtime = create_database_runtime(
        DatabaseSettings.from_mapping(
            {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "owner_url")}
        )
    )
    app_runtime = create_database_runtime(
        DatabaseSettings.from_mapping(
            {"DIRECTPILOT_DATABASE_URL": getattr(postgres_service, "app_url")}
        )
    )
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        identity = PostgresIdentityRepository(app_runtime.sessions)
        created = identity.create_personal_workspace(
            display_name="Synthetic authorization owner",
            workspace_name="Synthetic authorization workspace",
            yandex_subject="synthetic-authorization-owner-001",
        )
        authorizer = PostgresWorkspaceAuthorizer(app_runtime.sessions)

        allowed = authorizer.authorize(
            user_id=created.user.id,
            workspace_id=created.workspace.id,
            capability=Capability.MANAGE_MEMBERS,
        )
        assert allowed.role is MembershipRole.OWNER

        identity.update_membership(
            workspace_id=created.workspace.id,
            user_id=created.user.id,
            role=MembershipRole.VIEWER,
            status=MembershipStatus.ACTIVE,
        )
        with pytest.raises(AuthorizationDenied, match="workspace access is denied"):
            authorizer.authorize(
                user_id=created.user.id,
                workspace_id=created.workspace.id,
                capability=Capability.MANAGE_MEMBERS,
            )

        identity.update_membership(
            workspace_id=created.workspace.id,
            user_id=created.user.id,
            role=MembershipRole.VIEWER,
            status=MembershipStatus.REVOKED,
        )
        with pytest.raises(AuthorizationDenied, match="workspace membership is inactive"):
            authorizer.authorize(
                user_id=created.user.id,
                workspace_id=created.workspace.id,
                capability=Capability.READ_WORKSPACE_DATA,
            )
    finally:
        owner_runtime.close()
        app_runtime.close()
