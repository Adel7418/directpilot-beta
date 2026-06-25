"""Tests for the safe Yandex Direct ``ads.update`` BusinessId attach helper.

The endpoint ``POST /yandex/ads/business`` is the safe-by-default way
to attach an existing Yandex Business organization to one or more
existing DRAFT TextAd objects in a real Yandex Direct campaign. The
operation sets two v5 fields on each target ad:

* ``TextAd.BusinessId`` — long, the Yandex Business id
* ``TextAd.PreferVCardOverBusiness`` — ``"NO"`` (literal string per v5)

The endpoint follows the same gate contract as the rest of the
product:

* ``live_write`` is the only mode that may perform a real apply.
* ``live_readonly`` + ``dry_run=False`` is REJECTED before any
  network call (HTTP 409).
* ``dry_run=True`` is ALWAYS allowed and NEVER performs a network
  write.
* ``approved=True`` and ``idempotency_key`` (length >= 6) are required
  for real apply.
* Idempotency replay (same key) returns the cached result without
  re-sending.
* The OAUTH token is NEVER echoed back in any response body or
  error detail.
* The endpoint accepts either an explicit list of ``ad_ids`` OR a
  ``campaign_id``; when ``campaign_id`` is supplied, the store
  reads the campaign's ads via ``ads.get`` and targets only
  TextAds (``Ad.Type == "TEXT_AD"``).

These tests pin the contract end-to-end. Real Yandex ``ads.update``
calls are exercised via ``httpx.MockTransport`` only; no real
network is touched.
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

    return YandexDirectClient(settings=settings, transport=httpx.MockTransport(_transport))


def _ads_get_handler(
    *,
    campaign_id: int,
    ads: list[dict[str, Any]],
) -> Any:
    """Build a handler that responds to ``ads.get`` for the given campaign."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") != "get":
            return httpx.Response(200, json={"error": {"error_code": 99}})
        # return only the requested campaign's ads
        return httpx.Response(
            200,
            json={
                "result": {
                    "Ads": [a for a in ads if a.get("CampaignId") == campaign_id]
                }
            },
        )

    return handler


def _ads_update_capture_handler(
    *,
    update_response: dict[str, Any] | None = None,
    captured: dict[str, Any] | None = None,
    on_update: Any = None,
    on_get: Any = None,
) -> Any:
    """Build a handler that captures ads.update calls and answers ads.get
    with a canned campaign-ads list.
    """
    captured_ref = captured if captured is not None else {}
    response = update_response or {"result": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        method = body.get("method")
        if method == "get":
            if on_get is not None:
                return on_get(request, body)
            return httpx.Response(
                200,
                json={"result": {"Ads": captured_ref.get("campaign_ads", [])}},
            )
        if method == "update":
            captured_ref.setdefault("update_urls", []).append(str(request.url))
            captured_ref.setdefault("update_bodies", []).append(body)
            if on_update is not None:
                on_update(request, body)
            return httpx.Response(200, json=response)
        return httpx.Response(200, json={"error": {"error_code": 99}})

    return handler


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "approved": True,
        "idempotency_key": "ads-biz-001",
        "dry_run": True,
        "business_id": 11588384335,
        "ad_ids": [99001, 99002, 99003],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# client helper (YandexDirectClient.ads_update)
# ---------------------------------------------------------------------------


def test_ads_update_posts_to_v5_ads_with_method_update():
    """``ads_update`` MUST post to the v5 ``ads`` service with
    ``method=update`` and a list of ``Ads``. The response envelope
    must NOT echo the OAUTH token.
    """

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {}})

    settings = _settings("live_write", token="SECRET-AU-1")
    client_obj = _client_with_handler(settings, handler)
    client_obj.ads_update(
        [
            {
                "Id": 99001,
                "TextAd": {
                    "Title": "Title",
                    "Text": "Body",
                    "Href": "https://example.com/landing",
                    "BusinessId": 11588384335,
                    "PreferVCardOverBusiness": "NO",
                },
            }
        ]
    )

    assert captured["url"] == "https://api.direct.yandex.com/json/v5/ads"
    assert captured["authorization"] == "Bearer SECRET-AU-1"
    assert captured["body"]["method"] == "update"
    assert captured["body"]["params"]["Ads"][0]["Id"] == 99001
    assert captured["body"]["params"]["Ads"][0]["TextAd"]["BusinessId"] == 11588384335
    assert (
        captured["body"]["params"]["Ads"][0]["TextAd"]["PreferVCardOverBusiness"]
        == "NO"
    )
    assert "SECRET-AU-1" not in str(captured["body"])


def test_ads_update_uses_sandbox_base_url_when_mode_is_sandbox():
    """When the runtime mode is ``sandbox``, the helper MUST post to
    the sandbox base URL — the same contract as the other v5 write
    helpers (``ads_add``, ``adgroups_add``)."""

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"result": {}})

    settings = _settings("sandbox", token="SECRET-AU-SB")
    client_obj = _client_with_handler(settings, handler)
    client_obj.ads_update(
        [
            {
                "Id": 1,
                "TextAd": {
                    "Title": "T",
                    "Text": "B",
                    "Href": "https://example.com",
                    "BusinessId": 11588384335,
                    "PreferVCardOverBusiness": "NO",
                },
            }
        ]
    )
    assert captured["url"] == "https://api-sandbox.direct.yandex.com/json/v5/ads"


def test_ads_update_missing_token_raises_before_network_call():
    """The helper MUST raise ``YandexDirectError`` BEFORE the
    network call when the OAUTH token is missing — same contract as
    every other v5 write helper."""

    settings = Settings(_env_file=None, directpilot_mode="live_write", yandex_oauth_token=None)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called when token is missing")

    client_obj = _client_with_handler(settings, handler)
    with pytest.raises(Exception) as exc_info:
        client_obj.ads_update(
            [
                {
                    "Id": 1,
                    "TextAd": {
                        "Title": "T",
                        "Text": "B",
                        "Href": "h",
                        "BusinessId": 1,
                        "PreferVCardOverBusiness": "NO",
                    },
                }
            ]
        )
    from app.yandex_direct import YandexDirectError

    assert isinstance(exc_info.value, YandexDirectError)
    assert "YANDEX_OAUTH_TOKEN" in str(exc_info.value)


def test_ads_update_propagates_http_error_as_yandex_direct_error():
    """An HTTP 4xx/5xx from the v5 service must surface as
    :class:`YandexDirectError` with a redacted message — never as a
    raw token-bearing traceback."""

    from app.yandex_direct import YandexDirectError

    settings = _settings("live_write", token="SECRET-AU-ERR")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client_obj = _client_with_handler(settings, handler)
    with pytest.raises(YandexDirectError) as exc_info:
        client_obj.ads_update(
            [
                {
                    "Id": 1,
                    "TextAd": {
                        "Title": "T",
                        "Text": "B",
                        "Href": "h",
                        "BusinessId": 1,
                        "PreferVCardOverBusiness": "NO",
                    },
                }
            ]
        )
    assert "SECRET-AU-ERR" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# request model
# ---------------------------------------------------------------------------


def test_request_model_requires_business_id_ad_ids_or_campaign_id():
    """The request model must require at least one of ``ad_ids`` or
    ``campaign_id`` AND ``business_id``."""

    from app.models import YandexAdsBusinessAttachRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        YandexAdsBusinessAttachRequest(
            approved=True,
            idempotency_key="aaaaaaaa",
            dry_run=True,
            business_id=1,
        )
    # ad_ids is also OK on its own
    ok = YandexAdsBusinessAttachRequest(
        approved=True,
        idempotency_key="aaaaaaaa",
        dry_run=True,
        business_id=1,
        ad_ids=[1, 2, 3],
    )
    assert ok.ad_ids == [1, 2, 3]
    # campaign_id is also OK on its own
    ok2 = YandexAdsBusinessAttachRequest(
        approved=True,
        idempotency_key="aaaaaaaa",
        dry_run=True,
        business_id=1,
        campaign_id=710691939,
    )
    assert ok2.campaign_id == 710691939


def test_request_model_enforces_idempotency_key_min_length():
    from app.models import YandexAdsBusinessAttachRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        YandexAdsBusinessAttachRequest(
            approved=True,
            idempotency_key="short",  # < 6 chars
            dry_run=True,
            business_id=1,
            ad_ids=[1],
        )


# ---------------------------------------------------------------------------
# endpoint: dry-run
# ---------------------------------------------------------------------------


def test_endpoint_dry_run_in_live_readonly_never_calls_network_and_returns_preview():
    """In ``live_readonly``, ``dry_run=True`` is allowed and must NOT
    call Yandex. The endpoint must return a payload preview that
    surfaces the v5 ``ads.update`` body that WOULD be sent, with
    ``applied=False``."""

    settings = _settings("live_readonly", token="SECRET-LR-DR")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called on ads/business dry_run")

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/ads/business",
            json=_payload(ad_ids=[99001, 99002]),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["source"] == "yandex"
    assert body["mode"] == "live_readonly"
    assert body["business_id"] == 11588384335
    # Token never echoed.
    assert "SECRET-LR-DR" not in response.text
    # Payload preview is the v5 ``ads.update`` shape.
    preview = body["payload_preview"]
    assert preview["method"] == "ads.update"
    assert preview["params"]["Ads"][0]["Id"] == 99001
    assert preview["params"]["Ads"][0]["TextAd"]["BusinessId"] == 11588384335
    assert preview["params"]["Ads"][0]["TextAd"]["PreferVCardOverBusiness"] == "NO"
    # The dry-run preview MUST include the required TextAd fields
    # the docs require (Title / Text / Href) so the real call does
    # not surprise the operator with missing-field rejections.
    text_ad = preview["params"]["Ads"][0]["TextAd"]
    assert "Title" in text_ad
    assert "Text" in text_ad
    assert "Href" in text_ad


# ---------------------------------------------------------------------------
# endpoint: live_readonly must reject real apply before any network call
# ---------------------------------------------------------------------------


def test_endpoint_live_readonly_rejects_real_write_before_network():
    """In ``live_readonly``, ``dry_run=False`` is REJECTED before any
    network call. The response must mention ``live_readonly`` and
    must NOT echo the OAUTH token."""

    settings = _settings("live_readonly", token="SECRET-LR-BLOCK")
    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(200, json={"result": {}})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/ads/business",
            json=_payload(
                dry_run=False, idempotency_key="ads-biz-lr-block-001"
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    assert "live_readonly" in response.text
    assert "SECRET-LR-BLOCK" not in response.text
    assert network_called["calls"] == 0


# ---------------------------------------------------------------------------
# endpoint: live_write success path
# ---------------------------------------------------------------------------


def test_endpoint_live_write_calls_ads_update_with_correct_v5_payload_for_multiple_ads():
    """In ``live_write`` + ``approved`` + ``idempotency_key`` +
    ``dry_run=False``, the endpoint must dispatch one ``ads.update``
    call carrying ONE item per target ad. Each item MUST carry
    ``Id`` and a ``TextAd`` block with ``BusinessId`` and
    ``PreferVCardOverBusiness='NO'``. The response must include
    the new ``ad_ids`` and a fresh ``audit_id``."""

    settings = _settings("live_write", token="SECRET-LW-FULL")
    captured: dict[str, Any] = {
        "update_bodies": [],
        "update_urls": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "update":
            captured["update_bodies"].append(body)
            captured["update_urls"].append(str(request.url))
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(200, json={"error": {"error_code": 99}})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/ads/business",
            json=_payload(
                ad_ids=[99001, 99002, 99003],
                dry_run=False,
                idempotency_key="ads-biz-full-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert len(captured["update_bodies"]) == 1
    body = captured["update_bodies"][0]
    assert body["method"] == "update"
    assert body["params"]["Ads"]
    ids = [item["Id"] for item in body["params"]["Ads"]]
    assert ids == [99001, 99002, 99003]
    for item in body["params"]["Ads"]:
        text_ad = item["TextAd"]
        assert text_ad["BusinessId"] == 11588384335
        assert text_ad["PreferVCardOverBusiness"] == "NO"
        # TextAd is REPLACE-shaped on ads.update: required fields per
        # the v5 docs are Title / Text / Href; the helper MUST send
        # them so the operator does not get a 5000-class error
        # from Direct.
        assert "Title" in text_ad
        assert "Text" in text_ad
        assert "Href" in text_ad
    # No token in the captured body.
    assert "SECRET-LW-FULL" not in json.dumps(body, ensure_ascii=False)

    # Endpoint response shape.
    result = response.json()
    assert result["applied"] is True
    assert result["dry_run"] is False
    assert result["source"] == "yandex"
    assert result["mode"] == "live_write"
    assert result["ad_ids"] == [99001, 99002, 99003]
    assert result["business_id"] == 11588384335
    assert result["audit_id"].startswith("audit_")
    assert "SECRET-LW-FULL" not in response.text


def test_endpoint_live_write_with_campaign_id_reads_ads_and_targets_only_text_ads():
    """When ``campaign_id`` is supplied, the endpoint must read
    the campaign's ads via ``ads.get`` and target only ads with
    ``Type == "TEXT_AD"``. Other ad types (e.g. ``IMAGE_AD``) are
    SKIPPED with a clear message in the response so the operator
    knows which ads were excluded."""

    settings = _settings("live_write", token="SECRET-LW-CAMPAIGN")
    captured: dict[str, Any] = {
        "update_bodies": [],
        "campaign_ads": [
            {
                "Id": 1001,
                "CampaignId": 710691939,
                "AdGroupId": 2001,
                "Type": "TEXT_AD",
                "TextAd": {
                    "Title": "Заголовок 1",
                    "Text": "Текст 1",
                    "Href": "https://example.com/1",
                    "BusinessId": None,
                    "VCardId": None,
                },
            },
            {
                "Id": 1002,
                "CampaignId": 710691939,
                "AdGroupId": 2001,
                "Type": "TEXT_AD",
                "TextAd": {
                    "Title": "Заголовок 2",
                    "Text": "Текст 2",
                    "Href": "https://example.com/2",
                    "BusinessId": None,
                    "VCardId": None,
                },
            },
            {
                "Id": 1003,
                "CampaignId": 710691939,
                "AdGroupId": 2001,
                "Type": "IMAGE_AD",
            },
        ],
    }

    yandex = _client_with_handler(settings, _ads_update_capture_handler(captured=captured))
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/ads/business",
            json={
                "approved": True,
                "idempotency_key": "ads-biz-campaign-001",
                "dry_run": False,
                "business_id": 11588384335,
                "campaign_id": 710691939,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert len(captured["update_bodies"]) == 1
    body = captured["update_bodies"][0]
    ids = [item["Id"] for item in body["params"]["Ads"]]
    # Only TEXT_ADs are targeted. IMAGE_AD is skipped.
    assert ids == [1001, 1002]
    result = response.json()
    assert result["ad_ids"] == [1001, 1002]
    # Skip list surfaces the excluded ad types for visibility.
    assert any(
        item.get("ad_id") == 1003 and item.get("reason") == "not_text_ad"
        for item in result.get("skipped", [])
    )
    # The first TextAd was sourced from the live read; Title/Text/Href
    # come from the read, so the helper must NOT overwrite them with
    # placeholders.
    first = body["params"]["Ads"][0]["TextAd"]
    assert first["Title"] == "Заголовок 1"
    assert first["Text"] == "Текст 1"
    assert first["Href"] == "https://example.com/1"
    assert "SECRET-LW-CAMPAIGN" not in response.text


# ---------------------------------------------------------------------------
# endpoint: approval / idempotency
# ---------------------------------------------------------------------------


def test_endpoint_without_approved_is_rejected():
    """The endpoint must reject ``approved=False`` with HTTP 409 and
    must NOT call the network."""

    settings = _settings("live_write", token="SECRET-NOAPP")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called on unapproved apply")

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/ads/business",
            json=_payload(
                approved=False,
                dry_run=False,
                idempotency_key="ads-biz-noapp-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    assert "approval" in response.text.lower()
    assert "SECRET-NOAPP" not in response.text


def test_endpoint_idempotency_replay_returns_cached_result_without_re_sending():
    """A second call with the same ``idempotency_key`` MUST return
    the cached result and must NOT call ``ads.update`` again."""

    settings = _settings("live_write", token="SECRET-IDEMP")
    captured: dict[str, Any] = {"update_bodies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "update":
            captured["update_bodies"].append(body)
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(200, json={"error": {"error_code": 99}})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        first = client.post(
            "/yandex/ads/business",
            json=_payload(
                ad_ids=[99001, 99002],
                dry_run=False,
                idempotency_key="ads-biz-idemp-001",
            ),
        )
        second = client.post(
            "/yandex/ads/business",
            json=_payload(
                ad_ids=[99001, 99002],
                dry_run=False,
                idempotency_key="ads-biz-idemp-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    # The second call MUST NOT re-send to Yandex.
    assert len(captured["update_bodies"]) == 1
    # Both responses carry the same audit id (replay).
    assert first.json()["audit_id"] == second.json()["audit_id"]
    assert "SECRET-IDEMP" not in first.text
    assert "SECRET-IDEMP" not in second.text


def test_endpoint_audit_event_records_business_id_and_idempotency_key():
    """A successful apply MUST record an audit event that includes
    ``business_id``, ``idempotency_key``, ``ad_count`` and the
    resolved ``ad_ids``. The audit event must NEVER include the
    OAUTH token."""

    from app.store import store

    settings = _settings("live_write", token="SECRET-AUDIT")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {}})

    yandex = _client_with_handler(settings, handler)
    # Snapshot the audit log size before the request.
    before = len(store.audit_events)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/ads/business",
            json=_payload(
                ad_ids=[99001, 99002, 99003],
                dry_run=False,
                idempotency_key="ads-biz-audit-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text

    new_events = store.audit_events[before:]
    matching = [e for e in new_events if e.action == "yandex_ads_business_attach_requested"]
    assert matching, f"no audit event recorded; saw actions={[e.action for e in new_events]}"
    event = matching[-1]
    assert event.dry_run is False
    assert event.details is not None
    assert event.details.get("business_id") == 11588384335
    assert event.details.get("idempotency_key") == "ads-biz-audit-001"
    assert event.details.get("ad_count") == 3
    assert event.details.get("ad_ids") == [99001, 99002, 99003]
    # The audit details must NEVER include the OAUTH token (or any
    # of its allow-list substrings).
    assert "SECRET-AUDIT" not in json.dumps(event.details, ensure_ascii=False)


# ---------------------------------------------------------------------------
# endpoint: token-leak guard
# ---------------------------------------------------------------------------


def test_endpoint_response_body_never_contains_oauth_token():
    """Across dry-run and live-write responses, the OAUTH token MUST
    NEVER appear in the response body — regardless of which path
    was taken. The audit log (already checked above) and the
    transport body (already checked in the client-helper tests)
    are the other two surfaces; this one covers the endpoint
    response."""

    from app.store import store

    settings = _settings("live_write", token="SECRET-NO-LEAK-7K9P")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {}})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        dry = client.post("/yandex/ads/business", json=_payload(ad_ids=[99001]))
        apply_resp = client.post(
            "/yandex/ads/business",
            json=_payload(
                ad_ids=[99001],
                dry_run=False,
                idempotency_key="ads-biz-noleak-001",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert dry.status_code == 200
    assert apply_resp.status_code == 200
    assert "SECRET-NO-LEAK-7K9P" not in dry.text
    assert "SECRET-NO-LEAK-7K9P" not in apply_resp.text
    # And the audit log too.
    for event in store.audit_events:
        if event.details is not None:
            assert "SECRET-NO-LEAK-7K9P" not in json.dumps(
                event.details, ensure_ascii=False
            )


def test_endpoint_dry_run_audits_without_performing_network():
    """``dry_run=True`` MUST record a ``yandex_ads_business_attach_requested``
    audit event with ``dry_run=True`` and the redacted payload preview.
    No network call is performed."""

    from app.store import store

    settings = _settings("live_readonly", token="SECRET-DR-AUDIT")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called on dry_run")

    yandex = _client_with_handler(settings, handler)
    before = len(store.audit_events)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/yandex/ads/business",
            json=_payload(ad_ids=[99001, 99002]),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    new_events = store.audit_events[before:]
    matching = [e for e in new_events if e.action == "yandex_ads_business_attach_requested"]
    assert matching
    event = matching[-1]
    assert event.dry_run is True
    assert event.details is not None
    assert event.details.get("business_id") == 11588384335
    assert event.details.get("ad_count") == 2
    assert "SECRET-DR-AUDIT" not in json.dumps(event.details, ensure_ascii=False)
