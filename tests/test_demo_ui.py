from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def assert_demo_page(path: str, expected_text: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert expected_text in body
    assert "DirectPilot Beta" in body
    assert "live-запис" in body
    assert "token" not in body.lower()
    assert ".env" not in body


def test_demo_home_page_links_to_application_sections():
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "DirectPilot Beta" in body
    assert "API-first слой для Яндекс Директа" in body
    for path in [
        "/demo/yandex-status",
        "/demo/campaigns",
        "/demo/report",
        "/demo/recommendations",
        "/demo/tools",
        "/demo/security-approval",
    ]:
        assert f'href="{path}"' in body
    assert "token" not in body.lower()
    assert ".env" not in body


def test_demo_yandex_status_page_is_safe_and_read_only():
    assert_demo_page("/demo/yandex-status", "Direct API используется для real-data read-only доступа")


def test_demo_campaigns_page_shows_yandex_campaigns():
    assert_demo_page("/demo/campaigns", "Yandex campaigns")


def test_demo_report_page_shows_mock_metrics():
    assert_demo_page("/demo/report", "Сводный mock-отчёт")


def test_demo_recommendations_page_shows_approval_buttons_as_demo_only():
    assert_demo_page("/demo/recommendations", "рекомендации требуют явного approve/reject")


def test_demo_tools_page_shows_elama_inspired_mvp_tools():
    assert_demo_page("/demo/tools", "Инструменты из eLama-референса для MVP")


def test_demo_security_approval_flow_page_documents_no_live_writes():
    assert_demo_page("/demo/security-approval", "live-записи отключены")
