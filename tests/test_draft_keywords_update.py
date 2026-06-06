"""Tests for PATCH /campaign-drafts/{draft_id}/keywords endpoint.

The endpoint must:
- accept payload {"keywords": [...]}
- return updated CampaignDraft with the new keywords
- persist the change (visible in subsequent GET /campaign-drafts/{id})
- write audit event "campaign_draft_keywords_updated"
- return 404 for an unknown draft_id
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _create_draft() -> str:
    payload = {
        "business_type": "remont",
        "region": "Kazan",
        "monthly_budget": 45000,
        "landing_url": "https://example.ru/remont",
    }
    response = client.post("/campaign-drafts", json=payload)
    assert response.status_code == 200
    return response.json()["id"]


def test_patch_draft_keywords_returns_updated_draft():
    draft_id = _create_draft()
    new_keywords = [
        "ремонт квартир Казань недорого",
        "ремонт под ключ Казань",
        "отделка квартир Казань",
    ]

    response = client.patch(
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": new_keywords},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == draft_id
    assert body["status"] == "draft"
    assert body["keywords"] == new_keywords


def test_patch_draft_keywords_persists_in_get():
    draft_id = _create_draft()
    new_keywords = ["косметический ремонт", "капитальный ремонт"]

    patch_resp = client.patch(
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": new_keywords},
    )
    assert patch_resp.status_code == 200

    get_resp = client.get(f"/campaign-drafts/{draft_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["keywords"] == new_keywords


def test_patch_draft_keywords_writes_audit_event():
    draft_id = _create_draft()
    new_keywords = ["сантехник Казань", "электрик Казань"]

    patch_resp = client.patch(
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": new_keywords},
    )
    assert patch_resp.status_code == 200

    log = client.get("/audit-log").json()["items"]
    matches = [
        event
        for event in log
        if event["action"] == "campaign_draft_keywords_updated"
        and event["entity"] == draft_id
    ]
    assert len(matches) == 1


def test_patch_draft_keywords_returns_404_for_unknown_draft():
    response = client.patch(
        "/campaign-drafts/draft_does_not_exist/keywords",
        json={"keywords": ["foo", "bar"]},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Campaign draft not found"
