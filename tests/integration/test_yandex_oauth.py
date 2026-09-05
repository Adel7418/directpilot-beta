from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from app.db.engine import DatabaseSettings, create_database_runtime
from app.db.migrations.runner import upgrade_database
from app.db.roles import bootstrap_database_roles
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.integrations.yandex.oauth import NewOAuthTransaction, hash_oauth_state
from app.modules.integrations.yandex.repository import (
    ExternalIdentityConflict,
    PostgresExternalIdentityRepository,
    PostgresOAuthTransactionRepository,
)
from app.modules.sessions.service import PostgresSessionService


@pytest.mark.integration
def test_durable_oauth_transaction_is_consumed_only_once(
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
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        identity = PostgresIdentityRepository(app_runtime.sessions).create_personal_workspace(
            display_name="Synthetic OAuth owner",
            workspace_name="Synthetic OAuth workspace",
        )
        browser_session = PostgresSessionService(app_runtime.sessions).issue(
            user_id=identity.user.id,
            active_workspace_id=identity.workspace.id,
            now=now,
        )
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(32)
        transactions = PostgresOAuthTransactionRepository(app_runtime.sessions)
        transactions.create(
            NewOAuthTransaction(
                state_hash=hash_oauth_state(state),
                code_verifier=verifier,
                user_id=identity.user.id,
                workspace_id=identity.workspace.id,
                browser_session_id=browser_session.id,
                return_path="/",
                created_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        )

        consumed = transactions.consume(
            state=state,
            user_id=identity.user.id,
            workspace_id=identity.workspace.id,
            browser_session_id=browser_session.id,
            now=now,
        )

        assert consumed is not None
        assert hmac.compare_digest(consumed.code_verifier, verifier)
        assert (
            transactions.consume(
                state=state,
                user_id=identity.user.id,
                workspace_id=identity.workspace.id,
                browser_session_id=browser_session.id,
                now=now,
            )
            is None
        )
    finally:
        owner_runtime.close()
        app_runtime.close()


@pytest.mark.integration
def test_external_identity_cannot_rebind_a_subject_or_local_user(
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
        identities = PostgresIdentityRepository(app_runtime.sessions)
        first = identities.create_personal_workspace(
            display_name="Synthetic first identity owner",
            workspace_name="Synthetic first identity workspace",
        )
        second = identities.create_personal_workspace(
            display_name="Synthetic second identity owner",
            workspace_name="Synthetic second identity workspace",
        )
        external_identities = PostgresExternalIdentityRepository(app_runtime.sessions)
        external_identities.bind_yandex_identity(
            user_id=first.user.id,
            workspace_id=first.workspace.id,
            issuer="https://login.yandex.ru",
            subject="stable-yandex-user-id",
            profile_login="synthetic-login",
            profile_display_name="Synthetic display name",
        )

        with pytest.raises(ExternalIdentityConflict):
            external_identities.bind_yandex_identity(
                user_id=second.user.id,
                workspace_id=second.workspace.id,
                issuer="https://login.yandex.ru",
                subject="stable-yandex-user-id",
                profile_login="synthetic-login",
                profile_display_name="Synthetic display name",
            )
        with pytest.raises(ExternalIdentityConflict):
            external_identities.bind_yandex_identity(
                user_id=first.user.id,
                workspace_id=first.workspace.id,
                issuer="https://login.yandex.ru",
                subject="different-stable-yandex-user-id",
                profile_login="synthetic-login",
                profile_display_name="Synthetic display name",
            )
    finally:
        owner_runtime.close()
        app_runtime.close()


@pytest.mark.integration
def test_durable_oauth_transaction_rejects_expiry_and_browser_mismatch(
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
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    try:
        bootstrap_database_roles(
            owner_runtime,
            app_password=getattr(postgres_service, "app_password"),
        )
        upgrade_database(owner_runtime)
        identity = PostgresIdentityRepository(app_runtime.sessions).create_personal_workspace(
            display_name="Synthetic rejected OAuth owner",
            workspace_name="Synthetic rejected OAuth workspace",
        )
        sessions = PostgresSessionService(app_runtime.sessions)
        bound_browser = sessions.issue(
            user_id=identity.user.id,
            active_workspace_id=identity.workspace.id,
            now=now,
        )
        other_browser = sessions.issue(
            user_id=identity.user.id,
            active_workspace_id=identity.workspace.id,
            now=now,
        )
        transactions = PostgresOAuthTransactionRepository(app_runtime.sessions)
        expired_state = secrets.token_urlsafe(32)
        transactions.create(
            NewOAuthTransaction(
                state_hash=hash_oauth_state(expired_state),
                code_verifier=secrets.token_urlsafe(32),
                user_id=identity.user.id,
                workspace_id=identity.workspace.id,
                browser_session_id=bound_browser.id,
                return_path="/",
                created_at=now - timedelta(minutes=10),
                expires_at=now,
            )
        )
        mismatch_state = secrets.token_urlsafe(32)
        transactions.create(
            NewOAuthTransaction(
                state_hash=hash_oauth_state(mismatch_state),
                code_verifier=secrets.token_urlsafe(32),
                user_id=identity.user.id,
                workspace_id=identity.workspace.id,
                browser_session_id=bound_browser.id,
                return_path="/",
                created_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        )

        assert (
            transactions.consume(
                state=expired_state,
                user_id=identity.user.id,
                workspace_id=identity.workspace.id,
                browser_session_id=bound_browser.id,
                now=now,
            )
            is None
        )
        assert (
            transactions.consume(
                state=mismatch_state,
                user_id=identity.user.id,
                workspace_id=identity.workspace.id,
                browser_session_id=other_browser.id,
                now=now,
            )
            is None
        )
    finally:
        owner_runtime.close()
        app_runtime.close()
