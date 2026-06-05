from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_endpoint_returns_mode_and_safe_yandex_status():
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "directpilot-beta"
    assert body["mode"] in {"mock", "sandbox", "live_readonly"}
    assert "token" not in str(body).lower()


def test_openapi_exposes_agent_facing_endpoints():
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    assert "/campaigns" in paths
    assert "/reports/summary" in paths
    assert "/campaign-drafts" in paths
    assert "/recommendations" in paths
    assert "/recommendations/{recommendation_id}/approve" in paths
    assert "/recommendations/{recommendation_id}/reject" in paths
    assert "/actions/{action_id}/apply" in paths
    assert "/audit-log" in paths


def test_mock_campaigns_are_read_only_and_small_business_oriented():
    response = client.get("/campaigns")

    assert response.status_code == 200
    campaigns = response.json()["items"]
    assert campaigns[0]["source"] == "mock"
    assert campaigns[0]["business_type"] == "local_services"


def test_apply_requires_approval_and_idempotency_key():
    response = client.post("/actions/act_mock_pause_keyword/apply", json={"dry_run": True})

    assert response.status_code == 422

    response = client.post(
        "/actions/act_mock_pause_keyword/apply",
        json={"dry_run": True, "approved": False, "idempotency_key": "test-key"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Action requires explicit approval before apply"
