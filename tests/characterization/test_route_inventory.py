"""Small OpenAPI route inventory for the current-master baseline."""

from __future__ import annotations

import json
from pathlib import Path

from app.main import app


_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
_EXPECTED_ROUTES = {
    "/campaigns": "get",
    "/campaign-drafts": "post",
    "/campaign-drafts/{draft_id}": "patch",
    "/campaign-drafts/{draft_id}/keywords": "delete",
}


def _operation_count(schema: dict) -> int:
    return sum(
        1
        for path_item in schema["paths"].values()
        for method in path_item
        if method.lower() in _HTTP_METHODS
    )


def test_openapi_baseline_route_inventory_matches_current_snapshot() -> None:
    snapshot_path = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    generated = app.openapi()

    for schema in (snapshot, generated):
        assert schema["info"]["title"] == "DirectPilot Beta API"
        assert schema["info"]["version"] == "0.2.1"
        assert len(schema["paths"]) == 85
        assert _operation_count(schema) == 99
        assert schema.get("components", {}).get("securitySchemes", {}) == {}
        for route, method in _EXPECTED_ROUTES.items():
            assert method in schema["paths"][route]

    # This is a route manifest comparison, not a broad response-schema snapshot.
    assert set(generated["paths"]) == set(snapshot["paths"])
