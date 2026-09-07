"""Tests for the read-only Yandex Search API v2 Web Search client and route.

All provider interactions use httpx.MockTransport; this module never calls the
real Yandex API.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.config import Settings
from app.main import app
from app.yandex_web_search import (
    YandexWebSearchClient,
    YandexWebSearchError,
    YandexWebSearchMissingFolderError,
    YandexWebSearchMissingKeyError,
    YandexWebSearchValidationError,
)


SECRET_KEY = "test-web-search-key-placeholder"
SECRET_FOLDER = "test-web-search-folder-placeholder"
SENSITIVE_QUERY = "private user search query"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "yandex_search_api_key": SECRET_KEY,
        "yandex_search_folder_id": SECRET_FOLDER,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _encoded_xml(xml: str) -> dict[str, str]:
    return {"rawData": base64.b64encode(xml.encode("utf-8")).decode("ascii")}


def _empty_result_xml() -> str:
    return """<yandexsearch><response><found>0</found><results /></response></yandexsearch>"""


def _client(handler, **overrides: Any) -> YandexWebSearchClient:
    return YandexWebSearchClient(
        settings=_settings(**overrides),
        transport=httpx.MockTransport(handler),
    )


def _capture(status: int = 200, body: Any = None) -> tuple[dict[str, Any], Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status, json=body if body is not None else _encoded_xml(_empty_result_xml()))

    return captured, handler


def _override_client(client_obj: YandexWebSearchClient, settings: Settings | None = None) -> None:
    app.dependency_overrides[main_mod.get_settings] = lambda: settings or _settings()
    app.dependency_overrides[main_mod.get_yandex_web_search_client] = lambda: client_obj


def _clear_overrides() -> None:
    app.dependency_overrides.pop(main_mod.get_settings, None)
    app.dependency_overrides.pop(main_mod.get_yandex_web_search_client, None)


def test_web_search_posts_exact_v2_defaults_with_api_key_auth() -> None:
    captured, handler = _capture()
    client = _client(handler)

    result = client.search_web("ремонт")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://searchapi.api.cloud.yandex.net/v2/web/search"
    assert captured["headers"]["authorization"] == f"Api-Key {SECRET_KEY}"
    assert captured["body"] == {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": "ремонт",
            "familyMode": "FAMILY_MODE_MODERATE",
            "page": "0",
            "fixTypoMode": "FIX_TYPO_MODE_ON",
        },
        "sortSpec": {
            "sortMode": "SORT_MODE_BY_RELEVANCE",
            "sortOrder": "SORT_ORDER_DESC",
        },
        "groupSpec": {
            "groupMode": "GROUP_MODE_FLAT",
            "groupsOnPage": "10",
            "docsInGroup": "1",
        },
        "maxPassages": "2",
        "region": "225",
        "l10n": "LOCALIZATION_RU",
        "folderId": SECRET_FOLDER,
        "responseFormat": "FORMAT_XML",
    }
    assert result == {"query": "ремонт", "found": 0, "results": []}
    assert SECRET_KEY not in str(result)
    assert SECRET_FOLDER not in str(result)


def test_web_search_forwards_page_region_and_limit_as_provider_strings() -> None:
    captured, handler = _capture()
    client = _client(handler)

    client.search_web("сантехник", page=3, region="213", limit=25)

    assert captured["body"]["query"]["queryText"] == "сантехник"
    assert captured["body"]["query"]["page"] == "3"
    assert captured["body"]["region"] == "213"
    assert captured["body"]["groupSpec"]["groupsOnPage"] == "25"
    assert captured["body"]["groupSpec"]["docsInGroup"] == "1"
    assert isinstance(captured["body"]["query"]["page"], str)
    assert isinstance(captured["body"]["groupSpec"]["groupsOnPage"], str)
    assert isinstance(captured["body"]["groupSpec"]["docsInGroup"], str)
    assert isinstance(captured["body"]["maxPassages"], str)


@pytest.mark.parametrize(
    ("query_text", "page", "region", "limit"),
    [
        ("", 0, "225", 10),
        ("x" * 401, 0, "225", 10),
        ("ok", -1, "225", 10),
        ("ok", 0, "225", 0),
        ("ok", 0, "225", 101),
    ],
)
def test_web_search_rejects_invalid_client_arguments_before_network(
    query_text: str,
    page: int,
    region: str,
    limit: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        raise AssertionError("network must not be reached for invalid input")

    client = _client(handler)

    with pytest.raises(YandexWebSearchValidationError) as exc:
        client.search_web(query_text, page=page, region=region, limit=limit)

    message = str(exc.value)
    assert SECRET_KEY not in message
    assert SECRET_FOLDER not in message
    if query_text:
        assert query_text not in message


def test_missing_key_fails_closed_before_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        raise AssertionError("network must not be reached without an API key")

    client = _client(handler, yandex_search_api_key=None)

    with pytest.raises(YandexWebSearchMissingKeyError) as exc:
        client.search_web(SENSITIVE_QUERY)

    assert SECRET_KEY not in str(exc.value)
    assert SENSITIVE_QUERY not in str(exc.value)


def test_missing_folder_fails_closed_before_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        raise AssertionError("network must not be reached without a folder ID")

    client = _client(handler, yandex_search_folder_id=None)

    with pytest.raises(YandexWebSearchMissingFolderError) as exc:
        client.search_web(SENSITIVE_QUERY)

    assert SECRET_FOLDER not in str(exc.value)
    assert SENSITIVE_QUERY not in str(exc.value)


def test_web_search_normalizes_xml_docs_in_provider_order() -> None:
    xml = """
    <yandexsearch>
      <response>
        <found priority="all">2</found>
        <results>
          <grouping>
            <group>
              <doc>
                <url>https://example.com/one</url>
                <domain>example.com</domain>
                <title>First <hlword>result</hlword> title</title>
                <headline>First <hlword>headline</hlword></headline>
                <passages>
                  <passage>First <hlword>passage</hlword> text</passage>
                  <passage>Second passage</passage>
                </passages>
              </doc>
            </group>
            <group>
              <doc>
                <url>https://second.example/path</url>
                <domain>second.example</domain>
                <title>Second title</title>
                <headline />
              </doc>
            </group>
          </grouping>
        </results>
      </response>
    </yandexsearch>
    """
    captured, handler = _capture(body=_encoded_xml(xml))
    client = _client(handler)

    result = client.search_web("ремонт")

    assert captured["method"] == "POST"
    assert result == {
        "query": "ремонт",
        "found": 2,
        "results": [
            {
                "title": "First result title",
                "url": "https://example.com/one",
                "domain": "example.com",
                "headline": "First headline",
                "passages": ["First passage text", "Second passage"],
            },
            {
                "title": "Second title",
                "url": "https://second.example/path",
                "domain": "second.example",
                "headline": None,
                "passages": [],
            },
        ],
    }


def test_web_search_found_is_null_when_provider_value_is_not_an_integer() -> None:
    xml = """<yandexsearch><response><found>unknown</found><results /></response></yandexsearch>"""
    _, handler = _capture(body=_encoded_xml(xml))

    assert _client(handler).search_web("ремонт")["found"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"rawData": "not-valid-base64!"},
        {"rawData": base64.b64encode(b"\xff\xfe").decode("ascii")},
        {"rawData": base64.b64encode(b"<yandexsearch>").decode("ascii")},
        {},
    ],
)
def test_malformed_or_missing_raw_data_raises_a_safe_error(body: dict[str, str]) -> None:
    _, handler = _capture(body=body)
    client = _client(handler)

    with pytest.raises(YandexWebSearchError) as exc:
        client.search_web(SENSITIVE_QUERY)

    message = str(exc.value)
    assert SECRET_KEY not in message
    assert SECRET_FOLDER not in message
    assert SENSITIVE_QUERY not in message
    assert "rawData" not in message


def test_provider_xml_error_raises_a_safe_error() -> None:
    xml = """<yandexsearch><error code="ACCESS_DENIED">private provider detail</error></yandexsearch>"""
    _, handler = _capture(body=_encoded_xml(xml))
    client = _client(handler)

    with pytest.raises(YandexWebSearchError) as exc:
        client.search_web(SENSITIVE_QUERY)

    message = str(exc.value)
    assert "private provider detail" not in message
    assert SENSITIVE_QUERY not in message
    assert SECRET_KEY not in message


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_provider_http_failures_are_safe_and_redacted(status: int) -> None:
    body = {"message": f"provider said {SENSITIVE_QUERY}; Api-Key {SECRET_KEY}"}
    _, handler = _capture(status=status, body=body)
    client = _client(handler)

    with pytest.raises(YandexWebSearchError) as exc:
        client.search_web(SENSITIVE_QUERY)

    message = str(exc.value)
    assert str(status) in message
    assert "provider said" not in message
    assert SECRET_KEY not in message
    assert SENSITIVE_QUERY not in message


def test_transport_failure_is_safe_and_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Api-Key {SECRET_KEY}; query={SENSITIVE_QUERY}")

    client = _client(handler)

    with pytest.raises(YandexWebSearchError) as exc:
        client.search_web(SENSITIVE_QUERY)

    message = str(exc.value)
    assert SECRET_KEY not in message
    assert SENSITIVE_QUERY not in message
    assert "ConnectError" in message


def test_public_result_never_contains_provider_raw_data_or_xml() -> None:
    xml = """<yandexsearch><response><found>1</found><results /></response></yandexsearch>"""
    _, handler = _capture(body=_encoded_xml(xml))

    result = _client(handler).search_web("ремонт")

    assert "rawData" not in result
    assert "yandexsearch" not in str(result)
    assert "<response>" not in str(result)


def test_web_search_route_returns_typed_live_response() -> None:
    xml = """
    <yandexsearch><response><found>1</found><results><grouping><group><doc>
    <url>https://example.com/</url><domain>example.com</domain><title>Example</title>
    </doc></group></grouping></results></response></yandexsearch>
    """
    _, handler = _capture(body=_encoded_xml(xml))
    client_obj = _client(handler)
    _override_client(client_obj)
    try:
        response = TestClient(app).get("/search/web?query=%D1%80%D0%B5%D0%BC%D0%BE%D0%BD%D1%82")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["source"] == "yandex_search_api"
    assert body["live"] is True
    assert body["data"] == {
        "query": "ремонт",
        "found": 1,
        "results": [
            {
                "title": "Example",
                "url": "https://example.com/",
                "domain": "example.com",
                "headline": None,
                "passages": [],
            }
        ],
    }
    assert SECRET_KEY not in response.text
    assert SECRET_FOLDER not in response.text
    assert "rawData" not in response.text


@pytest.mark.parametrize(
    "settings_kwargs",
    [{"yandex_search_api_key": None}, {"yandex_search_folder_id": None}],
)
def test_web_search_route_returns_503_when_required_config_is_missing(
    settings_kwargs: dict[str, Any],
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        calls.append(request)
        return httpx.Response(200, json=_encoded_xml(_empty_result_xml()))

    settings = _settings(**settings_kwargs)
    client_obj = YandexWebSearchClient(settings=settings, transport=httpx.MockTransport(handler))
    _override_client(client_obj, settings)
    try:
        response = TestClient(app).get("/search/web?query=foo")
    finally:
        _clear_overrides()

    assert calls == []
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "yandex_search_web_not_configured",
        "message": "Yandex Search API Web Search is not configured",
    }
    assert SECRET_KEY not in response.text
    assert SECRET_FOLDER not in response.text


def test_web_search_route_returns_502_for_upstream_failure() -> None:
    body = {"message": f"upstream leak {SECRET_KEY}; {SENSITIVE_QUERY}"}
    _, handler = _capture(status=500, body=body)
    client_obj = _client(handler)
    _override_client(client_obj)
    try:
        response = TestClient(app).get("/search/web?query=foo")
    finally:
        _clear_overrides()

    assert response.status_code == 502
    assert response.json()["detail"] == {
        "code": "yandex_search_web_upstream_error",
        "message": "Yandex Search API Web Search is unavailable",
    }
    assert SECRET_KEY not in response.text
    assert SENSITIVE_QUERY not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/search/web?query=",
        "/search/web?query=" + "x" * 401,
        "/search/web?query=ok&page=-1",
        "/search/web?query=ok&region=",
        "/search/web?query=ok&region=" + "x" * 101,
        "/search/web?query=ok&limit=0",
        "/search/web?query=ok&limit=21",
    ],
)
def test_web_search_route_rejects_invalid_parameters_with_422(path: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
        raise AssertionError("network must not be reached for invalid route parameters")

    client_obj = _client(handler)
    _override_client(client_obj)
    try:
        response = TestClient(app).get(path)
    finally:
        _clear_overrides()

    assert response.status_code == 422, response.text


def test_openapi_includes_web_search_without_provider_secrets_or_raw_data() -> None:
    schema = TestClient(app).get("/openapi.json").json()
    operation = schema["paths"]["/search/web"]["get"]
    limit_parameter = next(parameter for parameter in operation["parameters"] if parameter["name"] == "limit")
    encoded_operation = json.dumps(operation)

    assert "200" in operation["responses"]
    assert "502" in operation["responses"]
    assert "503" in operation["responses"]
    assert "rawData" not in encoded_operation
    assert "folderId" not in encoded_operation
    assert "Authorization" not in encoded_operation
    assert "yandex_search_api_key" not in encoded_operation
    assert limit_parameter["schema"]["minimum"] == 1
    assert limit_parameter["schema"]["maximum"] == 20


def test_health_and_safe_settings_status_continue_to_mask_search_credentials() -> None:
    settings = _settings()
    status = settings.safe_status()

    assert SECRET_KEY not in str(status)
    assert SECRET_FOLDER not in str(status)
    assert status["search_configured"] is True

    app.dependency_overrides[main_mod.get_settings] = lambda: settings
    try:
        response = TestClient(app).get("/health")
    finally:
        _clear_overrides()

    assert response.status_code == 200, response.text
    assert SECRET_KEY not in response.text
    assert SECRET_FOLDER not in response.text
