from fastapi import FastAPI

from app.bootstrap.application import create_app


def test_create_app_preserves_existing_openapi_metadata() -> None:
    app = create_app()

    assert isinstance(app, FastAPI)
    assert app.title == "DirectPilot Beta API"
    assert app.version == "0.2.1"
    assert app.description == "Standalone API-first beta app for safe Yandex Direct automation."
