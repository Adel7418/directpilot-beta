from dataclasses import replace

from fastapi import Depends
from fastapi.testclient import TestClient

from app.bootstrap.application import create_app
from app.bootstrap.dependencies import (
    create_application_dependencies,
    get_yandex_client,
    legacy_store,
)
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
