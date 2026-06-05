from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_utm_generator_endpoint_returns_safe_yandex_cpc_tags():
    response = client.post(
        "/utm/generate",
        json={
            "landing_url": "https://example.ru/remont",
            "campaign": "remont_kazan",
            "content": "main_ad",
            "term": "remont posudomoek",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["url"].startswith("https://example.ru/remont?")
    assert "utm_source=yandex" in body["url"]
    assert "utm_medium=cpc" in body["url"]
    assert body["requires_approval"] is False


def test_budget_simulator_is_dry_run_only():
    response = client.post(
        "/simulations/budget",
        json={"daily_budget": 1000, "avg_cpc": 50, "conversion_rate": 5.0},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["estimated_clicks"] == 20
    assert body["estimated_conversions"] == 1.0


def test_campaign_audit_returns_elama_inspired_hygiene_checks():
    response = client.get("/audit/campaigns")

    assert response.status_code == 200
    checks = response.json()["items"]
    codes = {item["code"] for item in checks}
    assert {"missing_utm", "no_metrica_goal", "high_cpc"}.issubset(codes)
