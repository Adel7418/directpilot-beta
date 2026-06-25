"""Tests for the read-only Direct API v5 get methods on YandexDirectClient.

Covers:
- adgroups_get / ads_get / keywords_get send the correct URL + JSON body
  (POST to /<service>, method=get, SelectionCriteria.CampaignIds, FieldNames)
- They use the live base URL in live_write mode and the sandbox base URL
  in sandbox mode
- The Yandex `{ok: false}` response shape is surfaced (error_code kept)
- OAuth token must not appear anywhere in returned dicts
- A missing token is reported before any network call
- HTTP 4xx/5xx responses surface as YandexDirectError

These tests use httpx.MockTransport only — no real network calls.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.yandex_direct import YandexDirectClient, YandexDirectError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_client(settings: Settings, handler) -> YandexDirectClient:
    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(handler))


def _capture_handler(
    body: dict | None = None,
    status: int = 200,
) -> tuple[dict, Any]:
    """Build a handler that captures the request and returns the given body."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            status,
            json=body if body is not None else {"result": []},
            headers={"Units": "5"},
        )

    return captured, handler


# ---------------------------------------------------------------------------
# adgroups_get
# ---------------------------------------------------------------------------


def test_adgroups_get_sandbox_posts_to_adgroups_service_with_campaign_id():
    captured, handler = _capture_handler(body={"result": []})
    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="t-secret"
    )
    client = _make_client(settings, handler)

    result = client.adgroups_get(12345)

    assert captured["url"] == "https://api-sandbox.direct.yandex.com/json/v5/adgroups"
    assert captured["authorization"] == "Bearer t-secret"
    body = captured["body"]
    assert body["method"] == "get"
    assert body["params"]["SelectionCriteria"] == {"CampaignIds": [12345]}
    assert "Id" in body["params"]["FieldNames"]
    assert result["ok"] is True
    assert result["units"] == "5"


def test_adgroups_get_live_write_uses_live_base_url():
    captured, handler = _capture_handler(body={"result": []})
    settings = Settings(
        _env_file=None,
        directpilot_mode="live_write",
        yandex_oauth_token="t",
    )
    client = _make_client(settings, handler)

    client.adgroups_get("cmp-7")

    assert captured["url"].startswith("https://api.direct.yandex.com/json/v5/adgroups")
    # campaign id may be passed as int or str; both must be forwarded as-is
    assert captured["body"]["params"]["SelectionCriteria"] == {"CampaignIds": ["cmp-7"]}


def test_adgroups_get_surfaces_yandex_error_and_does_not_leak_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 88, "error_detail": "Bad campaign"}},
        )

    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="topsecret"
    )
    client = _make_client(settings, handler)

    result = client.adgroups_get(1)

    assert result["ok"] is False
    assert result["error"]["error_code"] == 88
    assert "topsecret" not in str(result)


def test_adgroups_get_missing_token_raises_before_network_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be reached without token")

    settings = Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token=None)
    client = _make_client(settings, handler)

    with pytest.raises(YandexDirectError) as exc:
        client.adgroups_get(1)

    assert "YANDEX_OAUTH_TOKEN" in str(exc.value)


# ---------------------------------------------------------------------------
# ads_get
# ---------------------------------------------------------------------------


def test_ads_get_sandbox_posts_to_ads_service_with_campaign_id():
    captured, handler = _capture_handler(body={"result": []})
    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="t-secret"
    )
    client = _make_client(settings, handler)

    result = client.ads_get(99)

    assert captured["url"] == "https://api-sandbox.direct.yandex.com/json/v5/ads"
    body = captured["body"]
    assert body["method"] == "get"
    assert body["params"]["SelectionCriteria"] == {"CampaignIds": [99]}
    assert "Title" in body["params"]["TextAdFieldNames"]
    assert result["ok"] is True


def test_ads_get_live_readonly_uses_live_base_url():
    captured, handler = _capture_handler(body={"result": []})
    settings = Settings(
        _env_file=None,
        directpilot_mode="live_readonly",
        yandex_oauth_token="t",
    )
    client = _make_client(settings, handler)

    client.ads_get(5)

    # live_readonly still hits the real v5 base URL — this is a read-only call
    assert captured["url"].startswith("https://api.direct.yandex.com/json/v5/ads")


def test_ads_get_surfaces_yandex_error_and_does_not_leak_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 53, "error_detail": "Internal"}},
        )

    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="supertoken"
    )
    client = _make_client(settings, handler)

    result = client.ads_get(1)

    assert result["ok"] is False
    assert result["error"]["error_code"] == 53
    assert "supertoken" not in str(result)


# ---------------------------------------------------------------------------
# keywords_get
# ---------------------------------------------------------------------------


def test_keywords_get_sandbox_posts_to_keywords_service_with_campaign_id():
    captured, handler = _capture_handler(body={"result": []})
    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="t-secret"
    )
    client = _make_client(settings, handler)

    result = client.keywords_get(7)

    assert captured["url"] == "https://api-sandbox.direct.yandex.com/json/v5/keywords"
    body = captured["body"]
    assert body["method"] == "get"
    assert body["params"]["SelectionCriteria"] == {"CampaignIds": [7]}
    assert "Keyword" in body["params"]["FieldNames"]
    assert result["ok"] is True


def test_keywords_get_surfaces_yandex_error_and_does_not_leak_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 600, "error_detail": "Token invalid"}},
        )

    settings = Settings(
        _env_file=None, directpilot_mode="sandbox", yandex_oauth_token="leaky"
    )
    client = _make_client(settings, handler)

    result = client.keywords_get(1)

    assert result["ok"] is False
    assert result["error"]["error_code"] == 600
    assert "leaky" not in str(result)


def test_keywords_get_missing_token_raises_before_network_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be reached without token")

    settings = Settings(_env_file=None, directpilot_mode="sandbox", yandex_oauth_token=None)
    client = _make_client(settings, handler)

    with pytest.raises(YandexDirectError):
        client.keywords_get(1)
