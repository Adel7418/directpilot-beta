import logging

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.bootstrap.application import create_app
from app.core.logging import redact_value
from app.core.request_context import get_request_context


REDACTION_FIXTURE_VALUE = "redaction-fixture-value"
REQUEST_ID = "request-context-test-001"


def test_api_v1_error_uses_safe_envelope_and_request_context(caplog) -> None:
    app = create_app()
    observed_request_ids: list[str] = []

    @app.get("/api/v1/unsafe")
    def unsafe_api_v1_route() -> None:
        observed_request_ids.append(get_request_context().request_id)
        raise HTTPException(status_code=400, detail={"oauth_token": REDACTION_FIXTURE_VALUE})

    caplog.set_level(logging.INFO, logger="directpilot.request")
    response = TestClient(app).get(
        "/api/v1/unsafe", headers={"X-Request-ID": REQUEST_ID}
    )

    assert response.status_code == 400
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert response.json() == {
        "error": {
            "code": "http_error",
            "message": "Request failed",
            "request_id": REQUEST_ID,
        }
    }
    assert observed_request_ids == [REQUEST_ID]
    assert REDACTION_FIXTURE_VALUE not in response.text
    assert REDACTION_FIXTURE_VALUE not in caplog.text
    assert "request_completed" in caplog.text


def test_legacy_error_response_is_not_wrapped() -> None:
    app = create_app()

    @app.get("/legacy-unsafe")
    def unsafe_legacy_route() -> None:
        raise HTTPException(status_code=400, detail={"oauth_token": REDACTION_FIXTURE_VALUE})

    response = TestClient(app).get("/legacy-unsafe")

    assert response.status_code == 400
    assert response.json() == {"detail": {"oauth_token": REDACTION_FIXTURE_VALUE}}


def test_redaction_removes_sensitive_mapping_values() -> None:
    assert redact_value({"oauth_token": REDACTION_FIXTURE_VALUE, "status": "ok"}) == {
        "oauth_token": "[REDACTED]",
        "status": "ok",
    }
