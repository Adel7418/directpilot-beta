from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from fastapi import Depends, Request

from app.config import Settings, get_settings
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


def create_application_dependencies() -> ApplicationDependencies:
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
