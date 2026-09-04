"""Shared isolated fixtures for characterization tests.

All settings instances opt out of ``.env`` loading. Provider-facing tests use
an in-process ``httpx.MockTransport`` or a fake that raises on any attempted
provider method, so this suite never reaches an external network.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.yandex_direct import YandexDirectClient


class ExplodingProvider:
    """Fail immediately if a route reaches a provider method."""

    def __init__(self) -> None:
        self.network_attempts: list[str] = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        self.network_attempts.append(name)
        raise AssertionError(f"provider I/O was attempted through {name}")


@pytest.fixture(autouse=True)
def _restore_dependency_overrides() -> Iterator[None]:
    """Keep FastAPI dependency overrides local to each characterization test."""

    previous = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def yandex_settings() -> Callable[..., Settings]:
    def _make(mode: str, *, oauth_token: str | None = None) -> Settings:
        return Settings(
            _env_file=None,
            directpilot_mode=mode,
            yandex_oauth_token=oauth_token,
        )

    return _make


@pytest.fixture
def override_dependencies() -> Callable[..., None]:
    def _install(settings: Settings, yandex_client: object | None = None) -> None:
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = lambda: yandex_client

    return _install


@pytest.fixture
def direct_client_factory() -> Callable[
    [Settings, Callable[[httpx.Request], httpx.Response]], YandexDirectClient
]:
    def _make(
        settings: Settings,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> YandexDirectClient:
        return YandexDirectClient(
            settings=settings,
            transport=httpx.MockTransport(handler),
        )

    return _make


@pytest.fixture
def exploding_provider() -> ExplodingProvider:
    return ExplodingProvider()
