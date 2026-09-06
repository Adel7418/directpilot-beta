from __future__ import annotations

from app.config import Settings
from app.yandex_direct import YandexDirectClient
from app.yandex_metrika import YandexMetrikaClient
from app.yandex_search_wordstat import YandexSearchWordstatClient


class DefaultDirectClientFactory:
    def create(self, settings: Settings) -> YandexDirectClient | None:
        if settings.directpilot_mode not in ("sandbox", "live_readonly", "live_write"):
            return None
        return YandexDirectClient(settings=settings)


class PublicDirectClientFactory:
    """Public-safe generic factory that cannot select a global credential."""

    def create(self, settings: Settings) -> YandexDirectClient | None:
        del settings
        return None


class DefaultMetrikaClientFactory:
    def create(self, settings: Settings) -> YandexMetrikaClient:
        return YandexMetrikaClient(settings=settings)


class DefaultWordstatClientFactory:
    def create(self, settings: Settings) -> YandexSearchWordstatClient:
        return YandexSearchWordstatClient(settings=settings)
