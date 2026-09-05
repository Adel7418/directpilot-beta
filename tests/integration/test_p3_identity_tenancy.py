from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.models import UserRecord
from app.db.roles import bootstrap_database_roles
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.tenancy.models import MembershipRole


@pytest.mark.integration
def test_personal_workspace_and_owner_membership_are_atomic(
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
        repository = PostgresIdentityRepository(app_runtime.sessions)

        created = repository.create_personal_workspace(
            display_name="Synthetic owner",
            workspace_name="Synthetic workspace",
            yandex_subject="synthetic-owner-001",
        )

        assert created.membership.role is MembershipRole.OWNER
        assert created.membership.user_id == created.user.id
        assert created.membership.workspace_id == created.workspace.id

        with owner_runtime.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE memberships "
                    "ADD CONSTRAINT ck_p3_test_reject_membership "
                    "CHECK (false) NOT VALID"
                )
            )

        with pytest.raises(IntegrityError):
            repository.create_personal_workspace(
                display_name="Synthetic rollback",
                workspace_name="Synthetic rollback workspace",
                yandex_subject="synthetic-rollback-001",
            )

        with app_runtime.sessions() as session:
            remaining = session.scalar(
                select(func.count())
                .select_from(UserRecord)
                .where(UserRecord.yandex_subject == "synthetic-rollback-001")
            )
        assert remaining == 0
    finally:
        owner_runtime.close()
        app_runtime.close()
