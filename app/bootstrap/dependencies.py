from __future__ import annotations

import os
from dataclasses import dataclass
from typing import cast

from fastapi import Depends, Request

from app.config import Settings, get_settings
from app.db.engine import (
    DATABASE_URL_ENV,
    DatabaseRuntime,
    DatabaseSettings,
    create_database_runtime,
)
from app.db.schema import check_schema_compatibility
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.integrations.yandex.oauth import (
    YandexOAuthConfiguration,
    YandexOAuthIntegration,
)
from app.modules.integrations.yandex.repository import (
    PostgresExternalIdentityRepository,
    PostgresOAuthTransactionRepository,
)
from app.modules.sessions.service import PostgresSessionService
from app.modules.tenancy.authorization import PostgresWorkspaceAuthorizer
from app.providers.protocols import (
    DirectClientFactory,
    MetrikaClientFactory,
    WordstatClientFactory,
)
from app.providers.yandex import (
    DefaultDirectClientFactory,
    DefaultMetrikaClientFactory,
    DefaultWordstatClientFactory,
)
from app.repositories.context import RequestRepositoryProxy
from app.repositories.mock_store import MockStoreRepositoryAdapter
from app.repositories.postgres_store import PostgresLegacyStoreRepository
from app.repositories.protocols import LegacyStoreRepository
from app.store import store as mock_store
from app.yandex_direct import YandexDirectClient
from app.yandex_metrika import YandexMetrikaClient
from app.yandex_search_wordstat import YandexSearchWordstatClient

legacy_store_adapter = MockStoreRepositoryAdapter(mock_store)
legacy_store = RequestRepositoryProxy(legacy_store_adapter)


@dataclass(frozen=True, slots=True)
class ApplicationDependencies:
    repository: LegacyStoreRepository
    direct_client_factory: DirectClientFactory
    metrika_client_factory: MetrikaClientFactory
    wordstat_client_factory: WordstatClientFactory
    database_runtime: DatabaseRuntime | None = None
    identity_repository: PostgresIdentityRepository | None = None
    session_service: PostgresSessionService | None = None
    workspace_authorizer: PostgresWorkspaceAuthorizer | None = None
    yandex_oauth: YandexOAuthIntegration | None = None
    fake_auth_enabled: bool = False


def _fake_auth_is_enabled(app_env: str) -> bool:
    return app_env in {"local", "test"} and os.environ.get(
        "DIRECTPILOT_ENABLE_FAKE_AUTH", ""
    ).lower() == "1"


def _configured_yandex_oauth() -> YandexOAuthConfiguration | None:
    settings = get_settings()
    if settings.yandex_client_id is None or settings.yandex_client_secret is None:
        return None
    return YandexOAuthConfiguration(
        client_id=settings.yandex_client_id,
        client_secret=settings.yandex_client_secret,
        redirect_uri=settings.yandex_oauth_redirect_uri,
    )


def create_application_dependencies() -> ApplicationDependencies:
    database_url = os.environ.get(DATABASE_URL_ENV)
    app_env = os.environ.get("DIRECTPILOT_APP_ENV", "local").lower()
    if database_url:
        oauth_config = _configured_yandex_oauth()
        runtime = create_database_runtime(
            DatabaseSettings.from_mapping({DATABASE_URL_ENV: database_url})
        )
        try:
            check_schema_compatibility(runtime)
        except Exception:
            runtime.close()
            raise
        return ApplicationDependencies(
            repository=PostgresLegacyStoreRepository(runtime.sessions),
            direct_client_factory=DefaultDirectClientFactory(),
            metrika_client_factory=DefaultMetrikaClientFactory(),
            wordstat_client_factory=DefaultWordstatClientFactory(),
            database_runtime=runtime,
            identity_repository=PostgresIdentityRepository(runtime.sessions),
            session_service=PostgresSessionService(runtime.sessions),
            workspace_authorizer=PostgresWorkspaceAuthorizer(runtime.sessions),
            yandex_oauth=YandexOAuthIntegration(
                transactions=PostgresOAuthTransactionRepository(runtime.sessions),
                identities=PostgresExternalIdentityRepository(runtime.sessions),
                config=oauth_config,
            ),
            fake_auth_enabled=_fake_auth_is_enabled(app_env),
        )
    if app_env in {"production", "staging"}:
        DatabaseSettings.from_mapping({})
    return ApplicationDependencies(
        repository=legacy_store_adapter,
        direct_client_factory=DefaultDirectClientFactory(),
        metrika_client_factory=DefaultMetrikaClientFactory(),
        wordstat_client_factory=DefaultWordstatClientFactory(),
    )


def get_application_dependencies(request: Request) -> ApplicationDependencies:
    return cast(ApplicationDependencies, request.app.state.dependencies)


def get_repository(request: Request) -> LegacyStoreRepository:
    return get_application_dependencies(request).repository


def get_yandex_client(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> YandexDirectClient | None:
    return get_application_dependencies(request).direct_client_factory.create(settings)


def get_yandex_metrika_client(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> YandexMetrikaClient:
    return get_application_dependencies(request).metrika_client_factory.create(settings)


def get_yandex_search_wordstat_client(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> YandexSearchWordstatClient:
    return get_application_dependencies(request).wordstat_client_factory.create(settings)
