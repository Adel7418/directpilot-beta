from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_end_to_end_mock_mvp_flow_persists_state_and_audit_events():
    initial_log_count = len(client.get("/audit-log").json()["items"])

    draft_payload = {
        "business_type": "remont",
        "region": "Kazan",
        "monthly_budget": 45000,
        "landing_url": "https://example.ru/remont",
    }
    created = client.post("/campaign-drafts", json=draft_payload)
    assert created.status_code == 200
    draft = created.json()
    assert draft["id"].startswith("draft_")
    assert draft["status"] == "draft"
    assert "remont Kazan" in draft["keywords"]

    drafts = client.get("/campaign-drafts").json()["items"]
    assert any(item["id"] == draft["id"] for item in drafts)
    detail = client.get(f"/campaign-drafts/{draft['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == draft["id"]

    campaigns = client.get("/campaigns").json()["items"]
    assert any(item["id"] == draft["id"] and item["status"] == "draft" for item in campaigns)

    audit = client.get("/audit/campaigns").json()["items"]
    assert {"missing_utm", "no_metrica_goal", "high_cpc"}.issubset({item["code"] for item in audit})

    recommendations = client.get("/recommendations").json()["items"]
    assert recommendations
    rec = recommendations[0]
    assert rec["status"] == "pending"
    assert rec["action_id"]
    assert rec["reason"]
    assert rec["risk_level"] in {"low", "medium", "high"}

    forbidden = client.post(
        f"/actions/{rec['action_id']}/apply",
        json={"approved": False, "dry_run": False, "idempotency_key": "flow-key-1"},
    )
    assert forbidden.status_code == 409

    approved = client.post(f"/recommendations/{rec['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    applied = client.post(
        f"/actions/{rec['action_id']}/apply",
        json={"approved": True, "dry_run": False, "idempotency_key": "flow-key-1"},
    )
    assert applied.status_code == 200
    assert applied.json()["applied"] is True

    rec_after = client.get("/recommendations").json()["items"][0]
    assert rec_after["status"] == "applied"

    log = client.get("/audit-log").json()["items"]
    assert len(log) >= initial_log_count + 3
    assert any(event["action"] == "campaign_draft_created" and event["entity"] == draft["id"] for event in log)
    assert any(event["action"] == "recommendation_approved" and event["entity"] == rec["id"] for event in log)
    assert any(event["action"] == "action_applied" and event["entity"] == rec["action_id"] for event in log)


def test_apply_idempotency_key_returns_same_result_without_duplicate_audit_event():
    recs = client.get("/recommendations").json()["items"]
    rec = next(item for item in recs if item["status"] == "pending")
    client.post(f"/recommendations/{rec['id']}/approve")
    key = "idem-key-unique-1"
    before = client.get("/audit-log").json()["items"]

    first = client.post(
        f"/actions/{rec['action_id']}/apply",
        json={"approved": True, "dry_run": False, "idempotency_key": key},
    )
    second = client.post(
        f"/actions/{rec['action_id']}/apply",
        json={"approved": True, "dry_run": False, "idempotency_key": key},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()

    after = client.get("/audit-log").json()["items"]
    new_apply_events = [
        event for event in after[len(before):]
        if event["action"] == "action_applied" and event["entity"] == rec["action_id"]
    ]
    assert len(new_apply_events) == 1


def test_reject_changes_recommendation_state():
    recs = client.get("/recommendations").json()["items"]
    pending = next((item for item in recs if item["status"] == "pending"), None)
    assert pending is not None

    response = client.post(f"/recommendations/{pending['id']}/reject")

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    recs_after = client.get("/recommendations").json()["items"]
    assert any(item["id"] == pending["id"] and item["status"] == "rejected" for item in recs_after)
