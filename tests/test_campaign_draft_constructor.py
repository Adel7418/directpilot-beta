"""Tests for the campaign-draft constructor endpoints.

Covers: PATCH base, keywords add/remove, negative-keywords, ad-groups CRUD,
ads CRUD, generate-structure, validate, preview, budget, bids.

All tests run against the in-memory mock store (no live Yandex calls).
"""

from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)


def _create_draft(
    business_type: str = "remont",
    region: str = "Kazan",
    monthly_budget: float = 45000,
    landing_url: str = "https://example.ru/remont",
) -> str:
    payload = {
        "business_type": business_type,
        "region": region,
        "monthly_budget": monthly_budget,
        "landing_url": landing_url,
    }
    response = client.post("/campaign-drafts", json=payload)
    assert response.status_code == 200
    return response.json()["id"]


def _draft(draft_id: str):
    return store.drafts[draft_id]


# ---------------------------------------------------------------------------
# PATCH /campaign-drafts/{draft_id} (base settings)
# ---------------------------------------------------------------------------


def test_patch_draft_base_updates_only_supplied_fields():
    draft_id = _create_draft()

    response = client.patch(
        f"/campaign-drafts/{draft_id}",
        json={"name": "Ремонт Казань v2", "monthly_budget": 60000},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == draft_id
    assert body["name"] == "Ремонт Казань v2"
    assert body["monthly_budget"] == 60000
    # business_type / region unchanged
    assert body["business_type"] == "remont"
    assert body["region"] == "Kazan"
    # landing_url not in PATCH -> preserved
    assert body["landing_url"] == "https://example.ru/remont"


def test_patch_draft_base_404_for_unknown_draft():
    response = client.patch(
        "/campaign-drafts/draft_nope",
        json={"name": "x"},
    )
    assert response.status_code == 404


def test_patch_draft_base_writes_audit_event():
    draft_id = _create_draft()
    before = len(client.get("/audit-log").json()["items"])

    client.patch(f"/campaign-drafts/{draft_id}", json={"name": "New"})

    log = client.get("/audit-log").json()["items"]
    matches = [
        e for e in log[before:]
        if e["action"] == "campaign_draft_base_updated" and e["entity"] == draft_id
    ]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# POST /campaign-drafts/{draft_id}/keywords (append, dedup)
# ---------------------------------------------------------------------------


def test_patch_keywords_replaces_all_and_writes_audit_details():
    draft_id = _create_draft()
    new_keywords = ["ремонт кондиционеров Казань", "чистка кондиционеров Казань"]

    response = client.patch(
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": new_keywords},
    )

    assert response.status_code == 200
    assert response.json()["keywords"] == new_keywords
    log = client.get("/audit-log").json()["items"]
    matches = [
        e
        for e in log
        if e["action"] == "campaign_draft_keywords_updated" and e["entity"] == draft_id
    ]
    assert matches
    assert matches[-1]["details"]["count"] == 2


def test_post_keywords_appends_unique_phrases():
    draft_id = _create_draft()
    initial = list(_draft(draft_id).keywords)

    response = client.post(
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": ["ремонт квартир", "ремонт квартир", "отделка"]},
    )

    assert response.status_code == 200
    new_keywords = response.json()["keywords"]
    assert new_keywords == initial + ["ремонт квартир", "отделка"]
    assert len(new_keywords) == len(set(new_keywords))


def test_post_keywords_dedupes_against_existing():
    draft_id = _create_draft()
    client.patch(
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": ["кухня", "ванная"]},
    )

    response = client.post(
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": ["кухня", "спальня"]},
    )

    keywords = response.json()["keywords"]
    assert "кухня" in keywords
    assert "ванная" in keywords
    assert "спальня" in keywords
    assert keywords.count("кухня") == 1


def test_post_keywords_404_for_unknown_draft():
    response = client.post(
        "/campaign-drafts/draft_nope/keywords",
        json={"keywords": ["x"]},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /campaign-drafts/{draft_id}/keywords
# ---------------------------------------------------------------------------


def test_delete_keywords_removes_listed_phrases():
    draft_id = _create_draft()
    client.patch(
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": ["кухня", "ванная", "спальня"]},
    )

    response = client.request(
        "DELETE",
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": ["ванная"]},
    )

    assert response.status_code == 200
    keywords = response.json()["keywords"]
    assert "ванная" not in keywords
    assert "кухня" in keywords and "спальня" in keywords


def test_delete_keywords_is_idempotent():
    draft_id = _create_draft()
    client.patch(
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": ["a", "b", "c"]},
    )

    r1 = client.request(
        "DELETE",
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": ["a"]},
    )
    r2 = client.request(
        "DELETE",
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": ["a"]},
    )

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["keywords"] == r2.json()["keywords"]


# ---------------------------------------------------------------------------
# PATCH /campaign-drafts/{draft_id}/negative-keywords (replace)
# ---------------------------------------------------------------------------


def test_patch_negative_keywords_replaces_set():
    draft_id = _create_draft()
    client.patch(
        f"/campaign-drafts/{draft_id}/negative-keywords",
        json={"negative_keywords": ["своими руками", "бесплатно"]},
    )

    response = client.patch(
        f"/campaign-drafts/{draft_id}/negative-keywords",
        json={"negative_keywords": ["diy", "скачать"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body["negative_keywords"]) == {"diy", "скачать"}
    assert "своими руками" not in body["negative_keywords"]


# ---------------------------------------------------------------------------
# POST/PATCH/DELETE /campaign-drafts/{draft_id}/ad-groups
# ---------------------------------------------------------------------------


def test_create_ad_group_adds_group_to_draft():
    draft_id = _create_draft()

    response = client.post(
        f"/campaign-drafts/{draft_id}/ad-groups",
        json={"name": "Кухни", "keywords": ["кухня казань", "заказать кухню"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["ad_groups"]) == 1
    group = body["ad_groups"][0]
    assert group["id"].startswith("adg_")
    assert group["name"] == "Кухни"
    assert group["keywords"] == ["кухня казань", "заказать кухню"]


def test_update_ad_group_renames_and_replaces_keywords():
    draft_id = _create_draft()
    created = client.post(
        f"/campaign-drafts/{draft_id}/ad-groups",
        json={"name": "Кухни", "keywords": ["кухня"]},
    )
    group_id = created.json()["ad_groups"][0]["id"]

    response = client.patch(
        f"/campaign-drafts/{draft_id}/ad-groups/{group_id}",
        json={"name": "Кухни и ванные", "keywords": ["кухня", "ванная"]},
    )

    assert response.status_code == 200
    group = next(g for g in response.json()["ad_groups"] if g["id"] == group_id)
    assert group["name"] == "Кухни и ванные"
    assert group["keywords"] == ["кухня", "ванная"]


def test_delete_ad_group_removes_group_and_its_ads():
    draft_id = _create_draft()
    created = client.post(
        f"/campaign-drafts/{draft_id}/ad-groups",
        json={"name": "Кухни", "keywords": []},
    )
    group_id = created.json()["ad_groups"][0]["id"]
    client.post(
        f"/campaign-drafts/{draft_id}/ads",
        json={"ad_group_id": group_id, "title": "T", "text": "B", "landing_url": "https://example.ru/x"},
    )

    response = client.delete(f"/campaign-drafts/{draft_id}/ad-groups/{group_id}")

    assert response.status_code == 200
    body = response.json()
    assert all(g["id"] != group_id for g in body["ad_groups"])
    assert all(a["ad_group_id"] != group_id for a in body["ads"])


def test_update_unknown_ad_group_returns_404():
    draft_id = _create_draft()
    response = client.patch(
        f"/campaign-drafts/{draft_id}/ad-groups/adg_nope",
        json={"name": "x"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST/PATCH/DELETE /campaign-drafts/{draft_id}/ads
# ---------------------------------------------------------------------------


def test_create_ad_requires_existing_ad_group():
    draft_id = _create_draft()
    response = client.post(
        f"/campaign-drafts/{draft_id}/ads",
        json={"ad_group_id": "adg_missing", "title": "T", "text": "B", "landing_url": "https://example.ru/x"},
    )
    assert response.status_code == 404


def test_create_and_update_and_delete_ad():
    draft_id = _create_draft()
    g = client.post(
        f"/campaign-drafts/{draft_id}/ad-groups",
        json={"name": "Кухни", "keywords": []},
    ).json()["ad_groups"][0]

    created = client.post(
        f"/campaign-drafts/{draft_id}/ads",
        json={
            "ad_group_id": g["id"],
            "title": "Кухни на заказ",
            "text": "От 30 000 ₽",
            "landing_url": "https://example.ru/kuhni",
        },
    )
    assert created.status_code == 200
    ad_id = created.json()["ads"][0]["id"]
    assert created.json()["ads"][0]["title"] == "Кухни на заказ"

    updated = client.patch(
        f"/campaign-drafts/{draft_id}/ads/{ad_id}",
        json={"title": "Кухни на заказ — скидка"},
    )
    assert updated.status_code == 200
    ad = next(a for a in updated.json()["ads"] if a["id"] == ad_id)
    assert ad["title"] == "Кухни на заказ — скидка"

    deleted = client.delete(f"/campaign-drafts/{draft_id}/ads/{ad_id}")
    assert deleted.status_code == 200
    assert all(a["id"] != ad_id for a in deleted.json()["ads"])


# ---------------------------------------------------------------------------
# POST /campaign-drafts/{draft_id}/generate-structure
# ---------------------------------------------------------------------------


def test_generate_structure_creates_groups_keywords_and_ads():
    draft_id = _create_draft()

    response = client.post(
        f"/campaign-drafts/{draft_id}/generate-structure",
        json={"topic": "ремонт квартир", "region": "Казань", "group_count": 2, "keywords_per_group": 3},
    )

    assert response.status_code == 200
    body = response.json()
    groups = body["ad_groups"]
    assert len(groups) == 2
    for g in groups:
        assert len(g["keywords"]) == 3
    assert body["ads"], "ads draft list should not be empty"
    assert all(a["ad_group_id"] in {g["id"] for g in groups} for a in body["ads"])
    assert isinstance(body["negative_keywords"], list)
    assert "negative_keywords" in body


def test_generate_structure_keeps_existing_keywords():
    draft_id = _create_draft()
    client.patch(
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": ["ремонт казань"]},
    )

    response = client.post(
        f"/campaign-drafts/{draft_id}/generate-structure",
        json={"topic": "ремонт квартир", "region": "Казань", "group_count": 1, "keywords_per_group": 2},
    )

    keywords = response.json()["keywords"]
    assert "ремонт казань" in keywords
    # new generated keywords added
    assert len(keywords) > 1


def test_generate_structure_does_not_overwrite_draft_region():
    draft_id = _create_draft(region="Kazan")

    response = client.post(
        f"/campaign-drafts/{draft_id}/generate-structure",
        json={"topic": "ремонт квартир", "region": "Москва", "group_count": 1, "keywords_per_group": 1},
    )

    assert response.status_code == 200
    assert client.get(f"/campaign-drafts/{draft_id}").json()["region"] == "Kazan"


# ---------------------------------------------------------------------------
# POST /campaign-drafts/{draft_id}/validate
# ---------------------------------------------------------------------------


def test_validate_empty_draft_returns_errors():
    draft_id = _create_draft()

    response = client.post(f"/campaign-drafts/{draft_id}/validate")

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    codes = {i["code"] for i in body["issues"]}
    assert "missing_ad_groups" in codes
    assert "missing_ads" in codes
    assert "missing_budget" not in codes
    assert any(i["severity"] == "error" for i in body["issues"])


def test_validate_warns_when_no_negative_keywords():
    draft_id = _create_draft()
    g = client.post(
        f"/campaign-drafts/{draft_id}/ad-groups",
        json={"name": "G", "keywords": ["k"]},
    ).json()["ad_groups"][0]
    client.post(
        f"/campaign-drafts/{draft_id}/ads",
        json={"ad_group_id": g["id"], "title": "T", "text": "B", "landing_url": "https://example.ru/x"},
    )
    client.patch(
        f"/campaign-drafts/{draft_id}/budget",
        json={"daily_budget": 1000},
    )

    response = client.post(f"/campaign-drafts/{draft_id}/validate")

    codes = {i["code"] for i in response.json()["issues"]}
    assert "no_negative_keywords" in codes


def test_validate_detects_duplicate_keywords():
    draft_id = _create_draft()
    client.patch(
        f"/campaign-drafts/{draft_id}/keywords",
        json={"keywords": ["a", "a", "b"]},
    )
    g = client.post(
        f"/campaign-drafts/{draft_id}/ad-groups",
        json={"name": "G", "keywords": ["k"]},
    ).json()["ad_groups"][0]
    client.post(
        f"/campaign-drafts/{draft_id}/ads",
        json={"ad_group_id": g["id"], "title": "T", "text": "B", "landing_url": "https://example.ru/x"},
    )
    client.patch(
        f"/campaign-drafts/{draft_id}/budget",
        json={"daily_budget": 1000},
    )
    client.patch(
        f"/campaign-drafts/{draft_id}/negative-keywords",
        json={"negative_keywords": ["n1"]},
    )

    response = client.post(f"/campaign-drafts/{draft_id}/validate")

    codes = {i["code"] for i in response.json()["issues"]}
    assert "duplicate_keywords" in codes


# ---------------------------------------------------------------------------
# GET /campaign-drafts/{draft_id}/preview
# ---------------------------------------------------------------------------


def test_preview_returns_dry_run_payload_and_requires_approval():
    draft_id = _create_draft()
    g = client.post(
        f"/campaign-drafts/{draft_id}/ad-groups",
        json={"name": "G", "keywords": ["k"]},
    ).json()["ad_groups"][0]
    client.post(
        f"/campaign-drafts/{draft_id}/ads",
        json={"ad_group_id": g["id"], "title": "T", "text": "B", "landing_url": "https://example.ru/x"},
    )
    client.patch(
        f"/campaign-drafts/{draft_id}/budget",
        json={"daily_budget": 1000, "strategy": "max_clicks"},
    )

    response = client.get(f"/campaign-drafts/{draft_id}/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["draft_id"] == draft_id
    assert body["dry_run"] is True
    assert body["requires_approval"] is True
    payload = body["yandex_payload"]
    assert payload["method"] == "create" or "method" in payload
    assert "campaign" in payload or "ad_groups" in payload


# ---------------------------------------------------------------------------
# PATCH /campaign-drafts/{draft_id}/budget
# ---------------------------------------------------------------------------


def test_patch_budget_updates_settings():
    draft_id = _create_draft()

    response = client.patch(
        f"/campaign-drafts/{draft_id}/budget",
        json={"daily_budget": 1500, "strategy": "max_clicks"},
    )

    assert response.status_code == 200
    budget = response.json()["budget"]
    assert budget["daily_budget"] == 1500
    assert budget["strategy"] == "max_clicks"


# ---------------------------------------------------------------------------
# PATCH /campaign-drafts/{draft_id}/bids
# ---------------------------------------------------------------------------


def test_patch_bids_updates_max_cpc_and_keyword_bids():
    draft_id = _create_draft()

    response = client.patch(
        f"/campaign-drafts/{draft_id}/bids",
        json={"max_cpc": 35, "keyword_bids": {"кухня казань": 50}},
    )

    assert response.status_code == 200
    bids = response.json()["bids"]
    assert bids["max_cpc"] == 35
    assert bids["keyword_bids"] == {"кухня казань": 50}
