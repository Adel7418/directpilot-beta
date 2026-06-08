"""Non-product guard for the retired DirectPilot demo/UI surface.

The HTML home page and the 6 ``/demo/*`` pages were built for early
stakeholder reviews and are no longer part of the product surface. They must
not be reachable in the running app, must not be advertised in the OpenAPI
schema, and must not collide with real product endpoints.

This test file is the regression guard for that contract. It is intentionally
kept under the historical ``test_demo_ui`` module name so the test pipeline
still runs it; the assertions it makes are the opposite of the old
test_demo_ui suite (404 + OpenAPI absence, not 200 + HTML body).
"""

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


NON_PRODUCT_PATHS = [
    "/",
    "/demo/yandex-status",
    "/demo/campaigns",
    "/demo/report",
    "/demo/recommendations",
    "/demo/tools",
    "/demo/security-approval",
]


def test_non_product_paths_return_404_and_explicit_message():
    for path in NON_PRODUCT_PATHS:
        response = client.get(path)

        assert response.status_code == 404, (
            f"{path} must not be served as product surface; "
            f"got {response.status_code} {response.text[:200]}"
        )
        body = response.json()
        assert body["path"] == path
        assert "Not part of DirectPilot product surface" in body["detail"]


def test_non_product_paths_are_absent_from_openapi_schema():
    schema = client.get("/openapi.json").json()
    openapi_paths = set(schema["paths"].keys())

    for path in NON_PRODUCT_PATHS:
        assert path not in openapi_paths, (
            f"{path} must not leak into the OpenAPI schema; "
            f"found: {openapi_paths & set(NON_PRODUCT_PATHS)}"
        )


def test_non_product_paths_do_not_collide_with_real_product_routes():
    """The retired paths must not shadow live API endpoints.

    A regression here would be the most dangerous: a stale demo handler
    catching traffic intended for a real route. We assert that all currently
    registered application paths outside the non-product allow-list are the
    real product surface.
    """

    real_paths = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
    }

    for path in NON_PRODUCT_PATHS:
        assert path in real_paths, (
            f"non-product guard for {path} is not registered"
        )
        # The retired path must be the only registered handler for that exact
        # path (no shadowing of a future real product route with the same URL).
        matches = [p for p in real_paths if p == path]
        assert len(matches) == 1, (
            f"path {path} is registered {len(matches)} times: {matches}"
        )
