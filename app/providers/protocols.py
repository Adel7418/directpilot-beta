from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.config import Settings
from app.yandex_direct import YandexDirectClient
from app.yandex_metrika import YandexMetrikaClient
from app.yandex_search_wordstat import YandexSearchWordstatClient


@runtime_checkable
class DirectClientFactory(Protocol):
    def create(self, settings: Settings) -> YandexDirectClient | None: ...


@runtime_checkable
class MetrikaClientFactory(Protocol):
    def create(self, settings: Settings) -> YandexMetrikaClient: ...


@runtime_checkable
class WordstatClientFactory(Protocol):
    def create(self, settings: Settings) -> YandexSearchWordstatClient: ...
