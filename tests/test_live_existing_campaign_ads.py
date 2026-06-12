"""Tests for live existing-campaign ad-group ads endpoints.

Covers:
* ``POST /yandex/ad-groups/{ad_group_id}/ads`` — add text ads to live ad group
* ``POST /yandex/ads/moderate`` — send ads to moderation
* Client helpers: ``ads_moderate``, ``ads_get_by_ids``

Gate contract:
* ``live_write`` is the only mode that may perform a real apply.
* ``live_readonly`` + ``dry_run=False`` is REJECTED before any
  network call (HTTP 409).
* ``dry_run=True`` is ALWAYS allowed and NEVER performs a network
  write.
* ``approved=True`` and ``idempotency_key`` (length >= 6) are
  required for real apply.
* Idempotency replay (same key) returns the cached result.
* The OAUTH token is NEVER echoed back.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.yandex_direct import YandexDirectClient

client = TestClient(app)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(mode: str, token: str | None = "t-secret") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=token)


def _client_with_handler(settings: Settings, handler) -> YandexDirectClient:
    def _transport(request: httpx.Request) -> httpx.Response:
        return handler(request)

    transport = httpx.MockTransport(_transport)
    return YandexDirectClient(settings=settings, transport=transport)


def _override(settings: Settings, yandex_client: YandexDirectClient | None = None):
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex_client


def _clear_overrides():
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# client helpers: ads_moderate / ads_get_by_ids
# ---------------------------------------------------------------------------


def test_ads_moderate_posts_to_v5_ads_with_method_moderate():
    """``ads_moderate`` MUST post to v5 ``ads`` with ``method=moderate``
    and ``SelectionCriteria.Ids``."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"result": {"ModerateResults": [{"Id": 1, "Status": "MODERATION"}]}},
        )

    cl = _client_with_handler(_settings("live_write"), handler)
    result = cl.ads_moderate([101, 102])

    assert result["ok"] is True
    assert captured["url"] == "https://api.direct.yandex.com/json/v5/ads"
    assert captured["body"]["method"] == "moderate"
    assert captured["body"]["params"]["SelectionCriteria"]["Ids"] == [101, 102]
    # No token echo.
    assert "t-secret" not in str(result)


def test_ads_get_by_ids_uses_ids_in_selection_criteria():
    """``ads_get_by_ids`` uses ``SelectionCriteria.Ids`` for targeted read."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "result": {
                    "Ads": [
                        {"Id": 101, "AdGroupId": 1, "Status": "MODERATION"}
                    ]
                }
            },
        )

    cl = _client_with_handler(_settings("live_readonly"), handler)
    result = cl.ads_get_by_ids([101])

    assert result["ok"] is True
    assert captured["body"]["params"]["SelectionCriteria"]["Ids"] == [101]
    assert "TextAdFieldNames" in captured["body"]["params"]


# ---------------------------------------------------------------------------
# POST /yandex/ad-groups/{ad_group_id}/ads — dry_run gate
# ---------------------------------------------------------------------------


def test_ad_group_ads_add_dry_run_returns_preview_no_network():
    """dry_run=true must NEVER perform a network write and must return
    payload_preview with applied=False."""
    _override(_settings("live_write"))

    resp = client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "test-dry-001",
            "dry_run": True,
            "ads": [
                {
                    "title": "Заголовок",
                    "text": "Текст объявления",
                    "href": "https://example.com",
                }
            ],
        },
    )

    _clear_overrides()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["ad_group_id"] == "1001"
    assert body["payload_preview"] is not None
    assert body["payload_preview"]["method"] == "ads.add"
    assert len(body["warnings"]) == 3  # inheritance warnings


def test_ad_group_ads_add_dry_run_returns_warnings():
    """dry_run response must include inheritance warnings for BusinessId,
    SitelinkSetId, and keywords-not-per-ad."""
    _override(_settings("live_write"))

    resp = client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "test-warn-001",
            "dry_run": True,
            "ads": [
                {
                    "title": "T1",
                    "text": "Body",
                    "href": "https://example.com",
                }
            ],
        },
    )

    _clear_overrides()

    assert resp.status_code == 200
    warnings = resp.json()["warnings"]
    codes = {w["code"] for w in warnings}
    assert "inherit_business_id" in codes
    assert "inherit_sitelink_set_id" in codes
    assert "keywords_not_per_ad" in codes


# ---------------------------------------------------------------------------
# POST /yandex/ad-groups/{ad_group_id}/ads — apply gate
# ---------------------------------------------------------------------------


def test_ad_group_ads_add_live_readonly_with_dry_run_false_is_rejected():
    """live_readonly MUST reject dry_run=False before any network call (HTTP 409)."""
    _override(_settings("live_readonly"))

    resp = client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "test-rw-001",
            "dry_run": False,
            "ads": [
                {
                    "title": "T1",
                    "text": "Body",
                    "href": "https://example.com",
                }
            ],
        },
    )

    _clear_overrides()

    assert resp.status_code == 409, resp.text
    assert "live_write" in resp.json()["detail"]


def test_ad_group_ads_add_mock_with_dry_run_false_is_rejected():
    """mock mode MUST reject dry_run=False (HTTP 409)."""
    _override(_settings("mock"))

    resp = client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "test-mock-001",
            "dry_run": False,
            "ads": [
                {
                    "title": "T1",
                    "text": "Body",
                    "href": "https://example.com",
                }
            ],
        },
    )

    _clear_overrides()

    assert resp.status_code == 409, resp.text


def test_ad_group_ads_add_requires_approval():
    """approved=False must be rejected with HTTP 409."""
    _override(_settings("live_write"))

    resp = client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": False,
            "idempotency_key": "test-noap-001",
            "dry_run": True,
            "ads": [
                {
                    "title": "T1",
                    "text": "Body",
                    "href": "https://example.com",
                }
            ],
        },
    )

    _clear_overrides()

    assert resp.status_code == 409


def test_ad_group_ads_add_live_write_apply_succeeds():
    """live_write + approved + idempotency_key + dry_run=False must
    perform real ads.add and return ad_ids."""
    captured_add: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "add":
            captured_add["body"] = body
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AddResults": [
                            {"Id": 99001},
                            {"Id": 99002},
                        ]
                    }
                },
            )
        # readback (ads_get_by_ids)
        return httpx.Response(
            200,
            json={"result": {"Ads": [{"Id": 99001}, {"Id": 99002}]}},
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    resp = client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "test-apply-001",
            "dry_run": False,
            "ads": [
                {
                    "title": "Заголовок",
                    "text": "Текст",
                    "href": "https://example.com",
                },
                {
                    "title": "Заголовок 2",
                    "text": "Текст 2",
                    "href": "https://example.com/2",
                    "title2": "Второй заголовок",
                    "sitelink_set_id": 555,
                    "business_id": 12345,
                    "prefer_vcard_over_business": "NO",
                },
            ],
        },
    )

    _clear_overrides()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is False
    assert body["applied"] is True
    assert body["source"] == "yandex"
    assert body["ad_ids"] == [99001, 99002]
    assert len(body["add_results"]) == 2

    # Check v5 payload shape
    ads_sent = captured_add["body"]["params"]["Ads"]
    assert len(ads_sent) == 2
    assert ads_sent[0]["AdGroupId"] == 1001
    assert ads_sent[0]["TextAd"]["Title"] == "Заголовок"
    # Second ad has optional fields
    assert ads_sent[1]["TextAd"]["Title2"] == "Второй заголовок"
    assert ads_sent[1]["TextAd"]["SitelinkSetId"] == 555
    assert ads_sent[1]["TextAd"]["BusinessId"] == 12345
    assert ads_sent[1]["TextAd"]["PreferVCardOverBusiness"] == "NO"


def test_ad_group_ads_add_with_string_ad_group_id():
    """Non-numeric ad_group_id should still work — AdGroupId field is omitted
    from v5 payload since we can't parse it as int."""
    captured_add: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "add":
            captured_add["body"] = body
            return httpx.Response(
                200,
                json={"result": {"AddResults": [{"Id": 1}]}},
            )
        # readback
        return httpx.Response(
            200, json={"result": {"Ads": [{"Id": 1}]}}
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    resp = client.post(
        "/yandex/ad-groups/some-group-id/ads",
        json={
            "approved": True,
            "idempotency_key": "test-str-002",
            "dry_run": False,
            "ads": [
                {
                    "title": "T1",
                    "text": "Body",
                    "href": "https://example.com",
                }
            ],
        },
    )

    _clear_overrides()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    # AdGroupId omitted because "some-group-id" is not numeric
    assert "AdGroupId" not in captured_add["body"]["params"]["Ads"][0]


def test_ad_group_ads_add_idempotency_replay():
    """Same idempotency_key + dry_run mode MUST return cached result
    without re-sending to Yandex."""
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(
            200,
            json={"result": {"AddResults": [{"Id": 1}]}},
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    # First call
    resp1 = client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "idem-replay-002",
            "dry_run": False,
            "ads": [{"title": "T1", "text": "Body", "href": "https://example.com"}],
        },
    )
    # Second call with same key
    resp2 = client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "idem-replay-002",
            "dry_run": False,
            "ads": [{"title": "T1", "text": "Body", "href": "https://example.com"}],
        },
    )

    _clear_overrides()

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json() == resp2.json()
    # Handler fires for ads.add + readback on first call (2 calls);
    # second call is cached (0 additional calls).
    assert call_count[0] == 2


# ---------------------------------------------------------------------------
# POST /yandex/ads/moderate — dry_run gate
# ---------------------------------------------------------------------------


def test_ads_moderate_dry_run_returns_preview_no_network():
    """dry_run=true must return payload_preview with applied=False."""
    _override(_settings("live_write"))

    resp = client.post(
        "/yandex/ads/moderate",
        json={
            "approved": True,
            "idempotency_key": "mod-dry-001",
            "dry_run": True,
            "ad_ids": [101, 102],
        },
    )

    _clear_overrides()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["payload_preview"] is not None
    assert body["payload_preview"]["method"] == "ads.moderate"
    assert body["payload_preview"]["params"]["SelectionCriteria"]["Ids"] == [101, 102]


def test_ads_moderate_live_readonly_with_dry_run_false_is_rejected():
    """live_readonly MUST reject dry_run=False (HTTP 409)."""
    _override(_settings("live_readonly"))

    resp = client.post(
        "/yandex/ads/moderate",
        json={
            "approved": True,
            "idempotency_key": "mod-rw-001",
            "dry_run": False,
            "ad_ids": [101],
        },
    )

    _clear_overrides()

    assert resp.status_code == 409, resp.text
    assert "live_write" in resp.json()["detail"]


def test_ads_moderate_requires_approval():
    """approved=False must be rejected."""
    _override(_settings("live_write"))

    resp = client.post(
        "/yandex/ads/moderate",
        json={
            "approved": False,
            "idempotency_key": "mod-nap-001",
            "dry_run": True,
            "ad_ids": [101],
        },
    )

    _clear_overrides()

    assert resp.status_code == 409


def test_ads_moderate_requires_ad_ids():
    """Empty ad_ids must be rejected (validation)."""
    _override(_settings("live_write"))

    resp = client.post(
        "/yandex/ads/moderate",
        json={
            "approved": True,
            "idempotency_key": "mod-empty-001",
            "dry_run": True,
            "ad_ids": [],
        },
    )

    _clear_overrides()

    # Pydantic validation should reject empty list
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# POST /yandex/ads/moderate — apply gate
# ---------------------------------------------------------------------------


def test_ads_moderate_live_write_apply_succeeds():
    """live_write + approved + idempotency_key + dry_run=False must
    perform real ads.moderate and return moderate_results."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())

        # Also handle ads_get_by_ids readback
        body = json.loads(request.content.decode())
        if body.get("method") == "get":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "Ads": [
                            {"Id": 101, "Status": "MODERATION"},
                            {"Id": 102, "Status": "MODERATION"},
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "ModerateResults": [
                        {"Id": 101, "Status": "MODERATION"},
                        {"Id": 102, "Status": "MODERATION"},
                    ]
                }
            },
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    resp = client.post(
        "/yandex/ads/moderate",
        json={
            "approved": True,
            "idempotency_key": "mod-apply-001",
            "dry_run": False,
            "ad_ids": [101, 102],
        },
    )

    _clear_overrides()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is False
    assert body["applied"] is True
    assert body["source"] == "yandex"
    assert body["ad_ids"] == [101, 102]
    assert len(body["moderate_results"]) == 2
    assert body["readback"] is not None
    assert len(body["readback"]) == 2


def test_ads_moderate_idempotency_replay():
    """Same idempotency_key MUST return cached result."""
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(
            200,
            json={
                "result": {
                    "ModerateResults": [{"Id": 101, "Status": "MODERATION"}]
                }
            },
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    resp1 = client.post(
        "/yandex/ads/moderate",
        json={
            "approved": True,
            "idempotency_key": "mod-idem-002",
            "dry_run": False,
            "ad_ids": [101],
        },
    )
    resp2 = client.post(
        "/yandex/ads/moderate",
        json={
            "approved": True,
            "idempotency_key": "mod-idem-002",
            "dry_run": False,
            "ad_ids": [101],
        },
    )

    _clear_overrides()

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json() == resp2.json()
    # Handler fires for moderate + readback on first call (2 calls);
    # second call is cached (0 additional calls).
    assert call_count[0] == 2


# ---------------------------------------------------------------------------
# v5 payload shape for ads.add includes optional fields correctly
# ---------------------------------------------------------------------------


def test_ads_add_payload_omits_none_optional_fields():
    """Optional fields (Title2, SitelinkSetId, etc.) must be OMITTED when
    not provided — not sent as None."""
    captured_add: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "add":
            captured_add["body"] = body
            return httpx.Response(
                200,
                json={"result": {"AddResults": [{"Id": 1}]}},
            )
        # readback
        return httpx.Response(
            200, json={"result": {"Ads": [{"Id": 1}]}}
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "test-optional-002",
            "dry_run": False,
            "ads": [
                {
                    "title": "Minimal ad",
                    "text": "Just required fields",
                    "href": "https://example.com",
                }
            ],
        },
    )

    _clear_overrides()

    text_ad = captured_add["body"]["params"]["Ads"][0]["TextAd"]
    assert "Title" in text_ad
    assert "Text" in text_ad
    assert "Href" in text_ad
    # Optional fields absent
    assert "Title2" not in text_ad
    assert "SitelinkSetId" not in text_ad
    assert "BusinessId" not in text_ad
    assert "PreferVCardOverBusiness" not in text_ad
    assert "DisplayLinkPath" not in text_ad


def test_ads_add_payload_forbids_display_link_path():
    """DisplayLinkPath must NOT be sent — current Direct v5 rejects it."""
    captured_add: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "add":
            captured_add["body"] = body
            return httpx.Response(
                200,
                json={"result": {"AddResults": [{"Id": 1}]}},
            )
        # readback
        return httpx.Response(
            200, json={"result": {"Ads": [{"Id": 1}]}}
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    # We can't even send DisplayLinkPath because the model doesn't accept it.
    # This test ensures the field is NOT in the v5 payload shape.
    client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "test-dlp-002",
            "dry_run": False,
            "ads": [
                {
                    "title": "No DLP",
                    "text": "Body",
                    "href": "https://example.com",
                }
            ],
        },
    )

    _clear_overrides()

    text_ad = captured_add["body"]["params"]["Ads"][0]["TextAd"]
    assert "DisplayLinkPath" not in text_ad


# ---------------------------------------------------------------------------
# Error handling for YandexDirectError during apply
# ---------------------------------------------------------------------------


def test_ad_group_ads_add_yandex_error_returns_502():
    """A YandexDirectError during apply MUST return HTTP 502 with
    redacted message (no token echo)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error": {
                    "error_code": 1234,
                    "error_detail": "Some error",
                }
            },
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    resp = client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "test-err-001",
            "dry_run": False,
            "ads": [{"title": "T1", "text": "Body", "href": "https://example.com"}],
        },
    )

    _clear_overrides()

    assert resp.status_code == 502, resp.text
    assert "t-secret" not in resp.text


def test_ads_moderate_yandex_error_returns_502():
    """YandexDirectError during moderate apply MUST return HTTP 502."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_code": 5678}},
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    resp = client.post(
        "/yandex/ads/moderate",
        json={
            "approved": True,
            "idempotency_key": "mod-err-001",
            "dry_run": False,
            "ad_ids": [101],
        },
    )

    _clear_overrides()

    assert resp.status_code == 502, resp.text
    assert "t-secret" not in resp.text


# ---------------------------------------------------------------------------
# Provider warnings — Yandex Direct API v5 Warnings in response envelope
# ---------------------------------------------------------------------------

_JSON = __import__("json")


def test_call_preserves_yandex_warnings_in_envelope():
    """``_call`` MUST capture top-level ``Warnings`` from Yandex v5 response
    and return them alongside ``result`` and ``units``."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {"AddResults": [{"Id": 1}]},
                "Warnings": [
                    {
                        "Code": 10165,
                        "Message": "Параметр не будет применен",
                        "Details": "Параметр DisplayUrlPath не поддерживается",
                    }
                ],
            },
        )

    cl = _client_with_handler(_settings("live_write"), handler)
    result = cl.ads_add([{"AdGroupId": 1, "TextAd": {"Title": "T", "Text": "B", "Href": "https://example.com"}}])

    assert result["ok"] is True
    assert result["warnings"] is not None
    assert len(result["warnings"]) == 1
    assert result["warnings"][0]["Code"] == 10165
    assert result["warnings"][0]["Message"] == "Параметр не будет применен"
    assert "Параметр DisplayUrlPath" in result["warnings"][0]["Details"]
    assert "t-secret" not in _JSON.dumps(result)


def test_call_no_warnings_key_means_empty_list():
    """When Yandex response has no ``Warnings`` key, ``_call`` returns
    ``warnings: []`` (not None)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"AddResults": [{"Id": 1}]}},
        )

    cl = _client_with_handler(_settings("live_write"), handler)
    result = cl.ads_add([{"AdGroupId": 1, "TextAd": {"Title": "T", "Text": "B", "Href": "https://example.com"}}])

    assert result["ok"] is True
    assert result["warnings"] == []


def test_ad_group_ads_add_apply_includes_provider_warnings():
    """Real ads.add apply with Yandex warnings MUST return them as
    ``provider_warnings`` in the response."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = _JSON.loads(request.content.decode())
        if body.get("method") == "add":
            return httpx.Response(
                200,
                json={
                    "result": {"AddResults": [{"Id": 99001}]},
                    "Warnings": [
                        {
                            "Code": 10165,
                            "Message": "Параметр не будет применен",
                            "Details": "Параметр DisplayUrlPath не поддерживается в ads.add",
                        }
                    ],
                },
            )
        # readback (ads_get_by_ids)
        return httpx.Response(
            200,
            json={"result": {"Ads": [{"Id": 99001, "Status": "DRAFT", "AdGroupId": 1001}]}},
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    resp = client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "test-pw-001",
            "dry_run": False,
            "ads": [{"title": "T1", "text": "Body", "href": "https://example.com"}],
        },
    )

    _clear_overrides()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["ad_ids"] == [99001]
    assert "provider_warnings" in body
    assert len(body["provider_warnings"]) == 1
    assert body["provider_warnings"][0]["code"] == 10165
    assert "Параметр не будет применен" in body["provider_warnings"][0]["message"]
    assert "DisplayUrlPath" in body["provider_warnings"][0]["details"]
    assert "t-secret" not in resp.text


def test_ad_group_ads_add_audit_includes_provider_warnings():
    """Audit details for applied ads.add MUST include ``provider_warnings``
    redacted envelope."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = _JSON.loads(request.content.decode())
        if body.get("method") == "add":
            return httpx.Response(
                200,
                json={
                    "result": {"AddResults": [{"Id": 99001}]},
                    "Warnings": [
                        {
                            "Code": 10165,
                            "Message": "Параметр не будет применен",
                            "Details": "Поле DisplayUrlPath неизвестно сервису",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={"result": {"Ads": [{"Id": 99001}]}},
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    resp = client.post(
        "/yandex/ad-groups/1001/ads",
        json={
            "approved": True,
            "idempotency_key": "test-pw-audit-001",
            "dry_run": False,
            "ads": [{"title": "T1", "text": "Body", "href": "https://example.com"}],
        },
    )

    _clear_overrides()

    assert resp.status_code == 200, resp.text

    # Read audit log
    audit_resp = client.get("/audit-log")
    assert audit_resp.status_code == 200
    events = audit_resp.json()["items"]
    applied_events = [
        e for e in events
        if e["action"] == "yandex_ad_group_ads_add_applied"
    ]
    assert len(applied_events) >= 1
    details = applied_events[-1]["details"]
    assert "provider_warnings" in details
    assert len(details["provider_warnings"]) == 1
    assert details["provider_warnings"][0]["code"] == 10165
    assert "t-secret" not in str(details)


def test_ads_moderate_apply_includes_provider_warnings():
    """Real ads.moderate apply with Yandex warnings MUST return them as
    ``provider_warnings`` in the response."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = _JSON.loads(request.content.decode())
        if body.get("method") == "moderate":
            return httpx.Response(
                200,
                json={
                    "result": {"ModerateResults": [{"Id": 101, "Status": "MODERATION"}]},
                    "Warnings": [
                        {
                            "Code": 10165,
                            "Message": "Параметр не будет применен",
                            "Details": "Неизвестный параметр в запросе",
                        }
                    ],
                },
            )
        # readback
        return httpx.Response(
            200,
            json={"result": {"Ads": [{"Id": 101, "Status": "MODERATION"}]}},
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    resp = client.post(
        "/yandex/ads/moderate",
        json={
            "approved": True,
            "idempotency_key": "mod-pw-001",
            "dry_run": False,
            "ad_ids": [101],
        },
    )

    _clear_overrides()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert "provider_warnings" in body
    assert len(body["provider_warnings"]) == 1
    assert body["provider_warnings"][0]["code"] == 10165
    assert "t-secret" not in resp.text


def test_ads_moderate_audit_includes_provider_warnings():
    """Audit details for applied ads.moderate MUST include ``provider_warnings``."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = _JSON.loads(request.content.decode())
        if body.get("method") == "moderate":
            return httpx.Response(
                200,
                json={
                    "result": {"ModerateResults": [{"Id": 101, "Status": "MODERATION"}]},
                    "Warnings": [
                        {
                            "Code": 10165,
                            "Message": "Параметр не будет применен",
                            "Details": "Неизвестный параметр",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={"result": {"Ads": [{"Id": 101, "Status": "MODERATION"}]}},
        )

    settings = _settings("live_write")
    yc = _client_with_handler(settings, handler)
    _override(settings, yc)

    resp = client.post(
        "/yandex/ads/moderate",
        json={
            "approved": True,
            "idempotency_key": "mod-pw-audit-001",
            "dry_run": False,
            "ad_ids": [101],
        },
    )

    _clear_overrides()

    assert resp.status_code == 200, resp.text

    audit_resp = client.get("/audit-log")
    assert audit_resp.status_code == 200
    events = audit_resp.json()["items"]
    applied_events = [
        e for e in events
        if e["action"] == "yandex_ads_moderate_applied"
    ]
    assert len(applied_events) >= 1
    details = applied_events[-1]["details"]
    assert "provider_warnings" in details
    assert len(details["provider_warnings"]) == 1
    assert details["provider_warnings"][0]["code"] == 10165


def test_provider_warnings_no_token_leakage():
    """Provider warnings MUST NOT contain OAuth token or Authorization header."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {"AddResults": [{"Id": 1}]},
                "Warnings": [
                    {
                        "Code": 10165,
                        "Message": "Параметр не будет применен",
                        "Details": "Field not supported",
                    }
                ],
            },
        )

    cl = _client_with_handler(_settings("live_write", "my-secret-token"), handler)
    result = cl.ads_add([{"AdGroupId": 1, "TextAd": {"Title": "T", "Text": "B", "Href": "https://example.com"}}])

    result_str = _JSON.dumps(result)
    assert "my-secret-token" not in result_str
    assert "Bearer" not in result_str
    assert "Authorization" not in result_str
