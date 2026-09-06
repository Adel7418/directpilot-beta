from dataclasses import replace

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.bootstrap import dependencies as dependencies_module
from app.bootstrap.application import create_app
from app.bootstrap.dependencies import (
    create_application_dependencies,
    get_yandex_client,
    legacy_store,
)
from app.modules.integrations.yandex.credentials import CredentialConfigurationError
from app.repositories.mock_store import MockStoreRepositoryAdapter
from app.repositories.protocols import LegacyStoreRepository


class FakeStore:
    campaigns = {"campaign-1": "campaign"}
    drafts = {}
    recommendations = {}
    audit_events = []

    def create_draft(self, payload):
        return {"payload": payload}

    def append_audit(self, *args, **kwargs):
        return {"args": args, "kwargs": kwargs}

    def yandex_control(self, *args, **kwargs):
        return {"args": args, "kwargs": kwargs}


class FakeDirectClientFactory:
    def __init__(self) -> None:
        self.client = object()
        self.settings = None

    def create(self, settings):
        self.settings = settings
        return self.client


def test_mock_store_adapter_satisfies_repository_protocol() -> None:
    adapter = MockStoreRepositoryAdapter(FakeStore())

    assert isinstance(adapter, LegacyStoreRepository)
    assert adapter.campaigns == {"campaign-1": "campaign"}
    assert adapter.create_draft("draft") == {"payload": "draft"}


def test_factory_dependency_seam_uses_injected_direct_client_factory() -> None:
    direct_factory = FakeDirectClientFactory()
    dependencies = replace(
        create_application_dependencies(), direct_client_factory=direct_factory
    )
    app = create_app(dependencies=dependencies)

    @app.get("/factory-client")
    def factory_client(client=Depends(get_yandex_client)):
        return {"provided": client is direct_factory.client}

    response = TestClient(app).get("/factory-client")

    assert response.status_code == 200
    assert response.json() == {"provided": True}
    assert direct_factory.settings is not None
    assert app.state.dependencies is dependencies


def test_legacy_repository_proxy_uses_request_app_dependencies() -> None:
    repository = MockStoreRepositoryAdapter(FakeStore())
    dependencies = replace(create_application_dependencies(), repository=repository)
    app = create_app(dependencies=dependencies)

    @app.get("/repository-from-app")
    def repository_from_app():
        return {"campaign": legacy_store.campaigns["campaign-1"]}

    response = TestClient(app, raise_server_exceptions=False).get("/repository-from-app")

    assert response.status_code == 200
    assert response.json() == {"campaign": "campaign"}


def test_db_and_vault_expose_a_separate_connection_scoped_direct_factory(monkeypatch) -> None:
    class Runtime:
        sessions = object()

        def close(self) -> None:
            return None

    runtime = Runtime()
    monkeypatch.setenv(
        dependencies_module.DATABASE_URL_ENV,
        "postgresql+psycopg://app:synthetic@127.0.0.1:5432/directpilot",
    )
    monkeypatch.setattr(
        dependencies_module,
        "get_settings",
        lambda: dependencies_module.Settings(
            _env_file=None,
            credential_keyring_secret_file="test-keyring",
        ),
    )
    monkeypatch.setattr(dependencies_module, "create_database_runtime", lambda _settings: runtime)
    monkeypatch.setattr(dependencies_module, "check_schema_compatibility", lambda _runtime: None)
    monkeypatch.setattr(dependencies_module, "_configured_credential_vault", lambda _settings: object())

    dependencies = create_application_dependencies()

    assert dependencies.connection_scoped_direct_client_factory is not None
    assert dependencies.direct_client_factory.__class__.__name__ == "DefaultDirectClientFactory"


def test_db_without_vault_keeps_connection_scoped_factory_unavailable(monkeypatch) -> None:
    class Runtime:
        sessions = object()

        def close(self) -> None:
            return None

    monkeypatch.setenv(
        dependencies_module.DATABASE_URL_ENV,
        "postgresql+psycopg://app:synthetic@127.0.0.1:5432/directpilot",
    )
    monkeypatch.setattr(
        dependencies_module,
        "get_settings",
        lambda: dependencies_module.Settings(_env_file=None),
    )
    monkeypatch.setattr(
        dependencies_module,
        "create_database_runtime",
        lambda _settings: Runtime(),
    )
    monkeypatch.setattr(dependencies_module, "check_schema_compatibility", lambda _runtime: None)

    dependencies = create_application_dependencies()

    assert dependencies.connection_scoped_direct_client_factory is None
    assert dependencies.direct_client_factory.__class__.__name__ == "DefaultDirectClientFactory"


def test_db_oauth_without_vault_raises_before_exposing_dependencies_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        sessions = object()

        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    runtime = Runtime()
    monkeypatch.setenv(
        dependencies_module.DATABASE_URL_ENV,
        "postgresql+psycopg://app:synthetic@127.0.0.1:5432/directpilot",
    )
    monkeypatch.setattr(
        dependencies_module,
        "get_settings",
        lambda: dependencies_module.Settings(
            _env_file=None,
            yandex_client_id="synthetic-client-id",
            yandex_client_secret="synthetic-client-secret",
            yandex_oauth_token=None,
            credential_keyring_secret_file=None,
        ),
    )
    monkeypatch.setattr(
        dependencies_module,
        "create_database_runtime",
        lambda _settings: runtime,
    )

    with pytest.raises(CredentialConfigurationError):
        create_application_dependencies()

    assert runtime.close_calls == 1


def test_legacy_mode_keeps_connection_scoped_factory_unavailable(monkeypatch) -> None:
    monkeypatch.delenv(dependencies_module.DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv("DIRECTPILOT_APP_ENV", "local")

    dependencies = create_application_dependencies()

    assert dependencies.connection_scoped_direct_client_factory is None
    assert dependencies.direct_client_factory.__class__.__name__ == "DefaultDirectClientFactory"
