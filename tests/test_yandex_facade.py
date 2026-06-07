"""Tests for the Yandex Direct read-only facade and limited control facade.

All endpoints must:
- be deterministic mocks
- set source="mock" and read_only=True (for read endpoints)
- never perform real network calls
- pause/resume: require approved=true + idempotency_key, default dry_run=True,
  write audit event, return applied=not dry_run

These tests run in mock mode so the contract "mock mode unchanged" is
verified end-to-end. Live-mode behavior is covered in test_live_control.py.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings
from app.store import store


@pytest.fixture(autouse=True)
def _force_mock_mode():
    """Pin directpilot_mode=mock for the legacy facade contract tests.

    The .env may say sandbox; this fixture re-installs a mock override
    before every test in this module, in case a previous module's
    dependency_overrides leaked.
    """
    _mock = Settings(_env_file=None, directpilot_mode="mock")
    app.dependency_overrides[get_settings] = lambda: _mock
    yield
    # Leave the override in place; other modules may reset it.

client = TestClient(app)


# ---------------------------------------------------------------------------
# Read-only facade
# ---------------------------------------------------------------------------


def test_yandex_campaigns_returns_mock_source_and_read_only_flag():
    response = client.get("/yandex/campaigns")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "mock"
    assert body["read_only"] is True
    assert isinstance(body["items"], list)
    assert body["items"], "expected at least one mock campaign"


def test_yandex_ad_groups_endpoint_shape():
    response = client.get("/yandex/campaigns/cmp_mock_local_services/ad-groups")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "mock"
    assert body["read_only"] is True
    assert isinstance(body["items"], list)
    if body["items"]:
        g = body["items"][0]
        assert {"id", "campaign_id", "name", "status"}.issubset(g.keys())


def test_yandex_ads_endpoint_shape():
    response = client.get("/yandex/campaigns/cmp_mock_local_services/ads")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "mock"
    assert body["read_only"] is True


def test_yandex_keywords_endpoint_shape():
    response = client.get("/yandex/campaigns/cmp_mock_local_services/keywords")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "mock"
    assert body["read_only"] is True


def test_yandex_reports_summary_endpoint_shape():
    response = client.get("/yandex/reports/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "mock"
    assert body["read_only"] is True
    assert {"spend", "clicks", "impressions", "ctr", "cpc"}.issubset(body.keys())


def test_yandex_search_queries_endpoint_shape():
    response = client.get("/yandex/reports/search-queries")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "mock"
    assert body["read_only"] is True
    assert isinstance(body["items"], list)


# ---------------------------------------------------------------------------
# Limited control facade (pause / resume)
# ---------------------------------------------------------------------------


def test_pause_requires_approved_flag():
    response = client.post(
        "/yandex/campaigns/cmp_mock_local_services/pause",
        json={"approved": False, "idempotency_key": "pause-key-001"},
    )
    assert response.status_code == 409


def test_pause_requires_idempotency_key():
    response = client.post(
        "/yandex/campaigns/cmp_mock_local_services/pause",
        json={"approved": True, "dry_run": True},
    )
    # missing required field idempotency_key -> 422 from pydantic validation
    assert response.status_code == 422


def test_pause_dry_run_default_returns_applied_false():
    response = client.post(
        "/yandex/campaigns/cmp_mock_local_services/pause",
        json={"approved": True, "idempotency_key": "pause-key-002"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "pause"
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["source"] == "mock"
    assert body["new_status"] == "paused"


def test_pause_with_dry_run_false_returns_applied_true():
    response = client.post(
        "/yandex/campaigns/cmp_mock_local_services/pause",
        json={"approved": True, "idempotency_key": "pause-key-003", "dry_run": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["dry_run"] is False


def test_pause_writes_audit_event():
    before = len(client.get("/audit-log").json()["items"])

    response = client.post(
        "/yandex/campaigns/cmp_mock_local_services/pause",
        json={"approved": True, "idempotency_key": "pause-key-audit"},
    )
    assert response.status_code == 200
    audit_id = response.json()["audit_id"]

    log = client.get("/audit-log").json()["items"]
    new_events = log[before:]
    assert any(e["action"] == "yandex_pause_requested" for e in new_events)
    assert any(e["id"] == audit_id for e in new_events)


def test_pause_idempotency_key_returns_same_audit_id():
    body = {"approved": True, "idempotency_key": "pause-key-idem", "dry_run": False}
    r1 = client.post("/yandex/campaigns/cmp_mock_local_services/pause", json=body)
    r2 = client.post("/yandex/campaigns/cmp_mock_local_services/pause", json=body)

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["audit_id"] == r2.json()["audit_id"]


def test_resume_endpoint_shape_and_audit():
    before = len(client.get("/audit-log").json()["items"])
    response = client.post(
        "/yandex/campaigns/cmp_mock_local_services/resume",
        json={"approved": True, "idempotency_key": "resume-key-001", "dry_run": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "resume"
    assert body["dry_run"] is False
    assert body["applied"] is True
    assert body["new_status"] == "active"

    log = client.get("/audit-log").json()["items"]
    assert any(
        e["action"] == "yandex_resume_requested" and e["entity"] == "cmp_mock_local_services"
        for e in log[before:]
    )


def test_pause_does_not_mutate_other_campaigns():
    before_status = store.campaigns["cmp_mock_local_services"].status
    response = client.post(
        "/yandex/campaigns/cmp_mock_local_services/pause",
        json={"approved": True, "idempotency_key": "pause-isolated", "dry_run": True},
    )
    assert response.status_code == 200
    assert store.campaigns["cmp_mock_local_services"].status == before_status
