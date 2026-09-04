from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.models import SessionRecord
from app.db.roles import bootstrap_database_roles
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.sessions.service import PostgresSessionService, SessionLifetime


@pytest.mark.integration
def test_opaque_session_rotates_revokes_and_enforces_both_expiries(
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
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        identity = PostgresIdentityRepository(app_runtime.sessions).create_personal_workspace(
            display_name="Synthetic session owner",
            workspace_name="Synthetic session workspace",
            yandex_subject="synthetic-session-owner-001",
        )
        sessions = PostgresSessionService(
            app_runtime.sessions,
            lifetime=SessionLifetime(idle=timedelta(minutes=10), absolute=timedelta(hours=1)),
        )

        issued = sessions.issue(
            user_id=identity.user.id,
            active_workspace_id=identity.workspace.id,
            now=now,
        )
        assert len(issued.token) >= 40
        assert len(issued.csrf_token) >= 40

        with app_runtime.sessions() as session:
            stored = session.scalar(select(SessionRecord).where(SessionRecord.id == issued.id))
        assert stored is not None
        assert stored.token_hash != issued.token
        assert stored.csrf_secret_hash != issued.csrf_token
        assert sessions.verify_csrf(issued.token, issued.csrf_token, now=now) is True

        rotated = sessions.rotate(issued.token, now=now + timedelta(minutes=1))
        assert rotated is not None
        assert rotated.token != issued.token
        assert sessions.authenticate(issued.token, now=now + timedelta(minutes=1)) is None
        assert sessions.authenticate(rotated.token, now=now + timedelta(minutes=2)) is not None

        sessions.revoke(rotated.token, now=now + timedelta(minutes=3))
        assert sessions.authenticate(rotated.token, now=now + timedelta(minutes=3)) is None

        idle = sessions.issue(user_id=identity.user.id, now=now)
        assert sessions.authenticate(idle.token, now=now + timedelta(minutes=11)) is None

        absolute = sessions.issue(user_id=identity.user.id, now=now)
        assert sessions.authenticate(absolute.token, now=now + timedelta(hours=1, seconds=1)) is None
    finally:
        owner_runtime.close()
        app_runtime.close()
