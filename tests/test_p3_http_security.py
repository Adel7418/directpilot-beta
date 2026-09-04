from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.bootstrap.application import create_app
from app.bootstrap.dependencies import _fake_auth_is_enabled, create_application_dependencies


@pytest.mark.parametrize(
    ("app_env", "enabled", "expected"),
    (
        ("local", "", False),
        ("test", "0", False),
        ("local", "1", True),
        ("test", "1", True),
        ("production", "1", False),
        ("staging", "1", False),
    ),
)
def test_fake_auth_requires_explicit_local_or_test_enablement(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
    enabled: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("DIRECTPILOT_ENABLE_FAKE_AUTH", enabled)

    assert _fake_auth_is_enabled(app_env) is expected


@pytest.mark.parametrize("app_env", ("production", "staging"))
def test_fake_auth_route_is_fail_closed_for_production_like_environments(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
) -> None:
    monkeypatch.setenv("DIRECTPILOT_ENABLE_FAKE_AUTH", "1")
    dependencies = replace(
        create_application_dependencies(),
        fake_auth_enabled=_fake_auth_is_enabled(app_env),
    )

    response = TestClient(create_app(dependencies=dependencies), base_url="https://testserver").post(
        "/api/v1/_test/identity/login",
        json={"display_name": "Synthetic production-like user"},
    )

    assert response.status_code == 404


def test_fake_auth_is_not_available_without_explicit_local_test_enablement() -> None:
    app = create_app()

    response = TestClient(app, base_url="https://testserver").post(
        "/api/v1/_test/identity/login",
        json={"display_name": "Synthetic user"},
    )

    assert response.status_code == 404
