"""Tests for the keyword bids update endpoint.

``POST /yandex/campaigns/{campaign_id}/bids`` — gated write path for
updating SearchBid / ContextBid via v5 ``keywordbids.set``.

* ``dry_run=True`` (default) is ALWAYS allowed and NEVER performs a network
  write. The response includes the exact v5 ``keywordbids.set`` payload
  that WOULD be sent, with ``applied=False``.
* In ``live_write`` mode, ``dry_run=False`` is allowed only when
  ``approved=True`` and a valid ``idempotency_key`` is supplied.
* In non-``live_write`` modes, ``dry_run=False`` is REJECTED (HTTP 409).
* ``approved=false`` is ALWAYS rejected (HTTP 409) before any action.
* Rubles → micros conversion: 250.0 RUB → 250_000_000 micros.
* Minimal v5 item shape: ``KeywordId`` + ``SearchBid``/``ContextBid``,
  no CampaignId / AdGroupId.
* Idempotency: same key replay returns cached result only when same scope
  (`dry_run`) and equivalent payload material; same key with different payload
  (including different `dry_run`) is rejected.
* Provider warnings (e.g. 10160) are surfaced.
* Readback is called after apply.
* No token leakage in responses or error details.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_settings, get_yandex_client
from app.store import store
from app.yandex_direct import YandexDirectClient

client = TestClient(app)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

SECRET_TOKEN = "TOPSECRET-KW-BIDS-001"
MOCK_CAMPAIGN_ID = "cmp_mock_local_services"


def _settings(mode: str, token: str | None = SECRET_TOKEN) -> Settings:
    return Settings(
        _env_file=None, directpilot_mode=mode, yandex_oauth_token=token
    )


def _client_with_handler(settings: Settings, handler) -> YandexDirectClient:
    def _transport(request: httpx.Request) -> httpx.Response:
        try:
            return handler(request)
        except AssertionError:
            raise

    transport = httpx.MockTransport(_transport)
    return YandexDirectClient(settings=settings, transport=transport)


def _reset_overrides():
    app.dependency_overrides.clear()


# --- Assertion helpers -------------------------------------------------------


def _assert_no_token_in_response(data: dict) -> None:
    """Fail if SECRET_TOKEN appears anywhere in the response dict."""
    dumped = json.dumps(data, default=str)
    assert SECRET_TOKEN not in dumped, "Token leaked in response"


def _assert_no_token_in_error(response) -> None:
    """Fail if token appears in the error response body."""
    try:
        body = response.json()
    except Exception:
        body = {"text": response.text}
    _assert_no_token_in_response(body)


# ---------------------------------------------------------------------------
# TDD Step 1: Rubles → micros conversion (model-level)
# ---------------------------------------------------------------------------


class TestRublesToMicrosConversion:
    """Tests for KeywordBidItem.to_direct_micros_item()."""

    def test_search_bid_only(self):
        from app.models import KeywordBidItem

        item = KeywordBidItem(keyword_id=123, search_bid_rub=250.0)
        result = item.to_direct_micros_item()
        assert result == {
            "KeywordId": 123,
            "SearchBid": 250_000_000,
        }

    def test_context_bid_only(self):
        from app.models import KeywordBidItem

        item = KeywordBidItem(keyword_id=456, context_bid_rub=100.0)
        result = item.to_direct_micros_item()
        assert result == {
            "KeywordId": 456,
            "ContextBid": 100_000_000,
        }
        # No AutotargetingSearchBidIsAuto when only context_bid_rub is set
        assert "AutotargetingSearchBidIsAuto" not in result

    def test_both_bids(self):
        from app.models import KeywordBidItem

        item = KeywordBidItem(
            keyword_id=789, search_bid_rub=50.0, context_bid_rub=30.0
        )
        result = item.to_direct_micros_item()
        assert result["KeywordId"] == 789
        assert result["SearchBid"] == 50_000_000
        assert result["ContextBid"] == 30_000_000
        assert "AutotargetingSearchBidIsAuto" not in result

    def test_autotargeting_auto_true_adds_yes_after_identification(self):
        from app.models import KeywordBidItem

        item = KeywordBidItem(
            keyword_id=999,
            search_bid_rub=100.0,
            autotargeting_search_bid_is_auto=True,
        )
        result = item.to_direct_micros_item(is_autotargeting=True)
        assert result["AutotargetingSearchBidIsAuto"] == "YES"

    def test_autotargeting_explicit_no_adds_no_after_identification(self):
        from app.models import KeywordBidItem

        item = KeywordBidItem(
            keyword_id=999,
            search_bid_rub=100.0,
            autotargeting_search_bid_is_auto=False,
        )
        result = item.to_direct_micros_item(is_autotargeting=True)
        assert result["AutotargetingSearchBidIsAuto"] == "NO"

    def test_fractional_rubles_rounds_correctly(self):
        from app.models import KeywordBidItem

        item = KeywordBidItem(keyword_id=1, search_bid_rub=0.30)
        result = item.to_direct_micros_item()
        assert result["SearchBid"] == 300_000

    def test_no_bid_raises_validation_error(self):
        from app.models import KeywordBidItem
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            KeywordBidItem(keyword_id=1)

    def test_duplicate_keyword_ids_raises(self):
        from app.models import KeywordBidItem, KeywordBidUpdateRequest
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            KeywordBidUpdateRequest(
                approved=True,
                idempotency_key="test-key-01",
                items=[
                    KeywordBidItem(keyword_id=1, search_bid_rub=10),
                    KeywordBidItem(keyword_id=1, search_bid_rub=20),
                ],
            )


# ---------------------------------------------------------------------------
# TDD Step 2: Safety gates — approved=false, mode checks
# ---------------------------------------------------------------------------


class TestSafetyGates:
    """Test that safety gates block writes BEFORE any network call."""

    def _make_request(self, **overrides) -> dict:
        body = {
            "approved": True,
            "idempotency_key": "gate-test-001",
            "dry_run": True,
            "items": [{"keyword_id": 1, "search_bid_rub": 100}],
        }
        body.update(overrides)
        return body

    def test_approved_false_rejected_409(self):
        """approved=false is always rejected BEFORE any network call."""
        body = self._make_request(approved=False)
        response = client.post(
            f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
        )
        assert response.status_code == 409
        assert "explicit approval" in response.json()["detail"]
        _assert_no_token_in_error(response)

    def test_live_readonly_blocks_real_write_409(self):
        """live_readonly mode blocks dry_run=false with HTTP 409."""
        app.dependency_overrides[get_settings] = lambda: _settings("live_readonly")

        body = self._make_request(dry_run=False)
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 409
            assert "live_write" in response.json()["detail"].lower()
            _assert_no_token_in_error(response)
        finally:
            _reset_overrides()

    def test_mock_mode_dry_run_allowed(self):
        """Mock mode + dry_run=True returns source=mock."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")

        body = self._make_request(dry_run=True)
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            data = response.json()
            assert data["source"] == "mock"
            assert data["dry_run"] is True
            assert data["applied"] is False
            assert data["payload_preview"] is not None
            _assert_no_token_in_response(data)
        finally:
            _reset_overrides()


# ---------------------------------------------------------------------------
# TDD Step 3: Payload shape — minimal v5 items
# ---------------------------------------------------------------------------


class TestPayloadShape:
    """Verify the v5 payload shape produced for dry_run."""

    def test_payload_has_keywordbids_set_shape(self):
        """payload_preview uses method=set with KeywordBids array."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")

        body = {
            "approved": True,
            "idempotency_key": "shape-test-001",
            "dry_run": True,
            "items": [
                {"keyword_id": 57440007797, "search_bid_rub": 250.0},
                {"keyword_id": 57440007798, "context_bid_rub": 100.0},
            ],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            data = response.json()
            preview = data["payload_preview"]
            assert preview["method"] == "set"
            items = preview["params"]["KeywordBids"]
            assert len(items) == 2

            # Item 1: SearchBid only
            item1 = items[0]
            assert item1["KeywordId"] == 57440007797
            assert item1["SearchBid"] == 250_000_000
            assert "ContextBid" not in item1
            assert "CampaignId" not in item1
            assert "AdGroupId" not in item1

            # Item 2: ContextBid only
            item2 = items[1]
            assert item2["KeywordId"] == 57440007798
            assert item2["ContextBid"] == 100_000_000
            assert "SearchBid" not in item2
            assert "CampaignId" not in item2
            assert "AdGroupId" not in item2

            _assert_no_token_in_response(data)
        finally:
            _reset_overrides()

    def test_two_manual_keywords_dry_run_only_changes_search_bid(self):
        """Manual keyword previews must not acquire autotargeting-only fields."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")
        body = {
            "approved": True,
            "idempotency_key": "manual-preview-500-001",
            "dry_run": True,
            "items": [
                {"keyword_id": 57373960029, "search_bid_rub": 500.0},
                {"keyword_id": 57373960030, "search_bid_rub": 500.0},
            ],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            assert response.json()["payload_preview"] == {
                "method": "set",
                "params": {
                    "KeywordBids": [
                        {"KeywordId": 57373960029, "SearchBid": 500_000_000},
                        {"KeywordId": 57373960030, "SearchBid": 500_000_000},
                    ]
                },
            }
        finally:
            _reset_overrides()

    def test_two_manual_keywords_apply_only_changes_search_bid(self):
        """The mocked writer and readback retain the manual-keyword field scope."""
        settings = _settings("live_write")
        calls = {"set": 0, "get": 0}

        def set_handler(request: httpx.Request) -> httpx.Response:
            calls["set"] += 1
            body = json.loads(request.content)
            assert body == {
                "method": "set",
                "params": {
                    "KeywordBids": [
                        {"KeywordId": 57373960029, "SearchBid": 500_000_000},
                        {"KeywordId": 57373960030, "SearchBid": 500_000_000},
                    ]
                },
            }
            return httpx.Response(
                200,
                json={
                    "result": {
                        "SetResults": [
                            {"Id": 57373960029},
                            {"Id": 57373960030},
                        ]
                    }
                },
                request=request,
            )

        def get_handler(request: httpx.Request) -> httpx.Response:
            calls["get"] += 1
            return _build_keywords_get_handler(
                [
                    {"Id": 57373960029, "Bid": 500_000_000, "ContextBid": 0},
                    {"Id": 57373960030, "Bid": 500_000_000, "ContextBid": 0},
                ]
            )(request)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(
                settings, _build_multi_handler(set_handler, get_handler=get_handler)
            )
        )
        body = {
            "approved": True,
            "idempotency_key": "manual-apply-500-001",
            "dry_run": False,
            "items": [
                {"keyword_id": 57373960029, "search_bid_rub": 500.0},
                {"keyword_id": 57373960030, "search_bid_rub": 500.0},
            ],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            assert response.json()["applied"] is True
            assert response.json()["readback"] == [
                {"KeywordId": 57373960029, "Bid": 500_000_000, "ContextBid": 0},
                {"KeywordId": 57373960030, "Bid": 500_000_000, "ContextBid": 0},
            ]
            assert calls == {"set": 1, "get": 1}
        finally:
            _reset_overrides()


class TestAutotargetingBidFieldScope:
    """Autotargeting-only bid fields require an explicit, verified target."""

    @pytest.mark.parametrize(
        ("requested_auto", "expected_value"),
        [(False, "NO"), (True, "YES")],
    )
    def test_explicit_autotargeting_choice_is_verified_before_writer(
        self, requested_auto: bool, expected_value: str
    ):
        settings = _settings("live_write")
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            url = str(request.url)
            if "/keywords" in url and body["method"] == "get":
                calls.append("classify" if not calls else "readback")
                if calls[-1] == "classify":
                    return httpx.Response(
                        200,
                        json={
                            "result": {
                                "Keywords": [
                                    {
                                        "Id": 7001,
                                        "Keyword": "---autotargeting",
                                    }
                                ]
                            }
                        },
                        request=request,
                    )
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "Keywords": [
                                {
                                    "Id": 7001,
                                    "Bid": 500_000_000,
                                    "ContextBid": 0,
                                }
                            ]
                        }
                    },
                    request=request,
                )
            if "/keywordbids" in url and body["method"] == "set":
                assert calls == ["classify"]
                calls.append("set")
                assert body == {
                    "method": "set",
                    "params": {
                        "KeywordBids": [
                            {
                                "KeywordId": 7001,
                                "SearchBid": 500_000_000,
                                "AutotargetingSearchBidIsAuto": expected_value,
                            }
                        ]
                    },
                }
                return httpx.Response(
                    200,
                    json={"result": {"SetResults": [{"Id": 7001}]}},
                    request=request,
                )
            pytest.fail(f"Unexpected request: {url} {body['method']}")
            return httpx.Response(500, json={}, request=request)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, handler)
        )
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids",
                json={
                    "approved": True,
                    "idempotency_key": f"auto-explicit-{expected_value.lower()}-001",
                    "dry_run": False,
                    "items": [
                        {
                            "keyword_id": 7001,
                            "search_bid_rub": 500.0,
                            "autotargeting_search_bid_is_auto": requested_auto,
                        }
                    ],
                },
            )
            assert response.status_code == 200
            assert response.json()["applied"] is True
            assert calls == ["classify", "set", "readback"]
        finally:
            _reset_overrides()

    def test_explicit_autotargeting_choice_rejects_ordinary_keyword(self):
        settings = _settings("live_write")
        calls = {"classify": 0, "set": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            url = str(request.url)
            if "/keywords" in url and body["method"] == "get":
                calls["classify"] += 1
                return httpx.Response(
                    200,
                    json={"result": {"Keywords": [{"Id": 7002, "Keyword": "manual key"}]}},
                    request=request,
                )
            if "/keywordbids" in url and body["method"] == "set":
                calls["set"] += 1
                pytest.fail("ordinary keyword must be rejected before keywordbids.set")
            pytest.fail(f"Unexpected request: {url} {body['method']}")
            return httpx.Response(500, json={}, request=request)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, handler)
        )
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids",
                json={
                    "approved": True,
                    "idempotency_key": "ordinary-auto-choice-001",
                    "dry_run": False,
                    "items": [
                        {
                            "keyword_id": 7002,
                            "search_bid_rub": 500.0,
                            "autotargeting_search_bid_is_auto": False,
                        }
                    ],
                },
            )
            assert response.status_code == 409
            assert calls == {"classify": 1, "set": 0}
        finally:
            _reset_overrides()

    def test_ambiguous_autotargeting_classification_blocks_writer(self):
        settings = _settings("live_write")
        calls = {"classify": 0, "set": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            url = str(request.url)
            if "/keywords" in url and body["method"] == "get":
                calls["classify"] += 1
                return httpx.Response(
                    200,
                    json={"result": {"Keywords": []}},
                    request=request,
                )
            if "/keywordbids" in url and body["method"] == "set":
                calls["set"] += 1
                pytest.fail("ambiguous target classification must block keywordbids.set")
            pytest.fail(f"Unexpected request: {url} {body['method']}")
            return httpx.Response(500, json={}, request=request)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, handler)
        )
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids",
                json={
                    "approved": True,
                    "idempotency_key": "ambiguous-auto-choice-001",
                    "dry_run": False,
                    "items": [
                        {
                            "keyword_id": 7003,
                            "search_bid_rub": 500.0,
                            "autotargeting_search_bid_is_auto": False,
                        }
                    ],
                },
            )
            assert response.status_code == 502
            assert calls == {"classify": 1, "set": 0}
        finally:
            _reset_overrides()


# ---------------------------------------------------------------------------
# TDD Step 4: Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Test idempotency replay behaviour."""

    def test_same_key_same_payload_returns_cached(self):
        """Replay with same key and same dry_run returns cached result."""
        app.dependency_overrides[get_settings] = lambda: _settings("mock")

        body = {
            "approved": True,
            "idempotency_key": "idem-replay-001",
            "dry_run": True,
            "items": [{"keyword_id": 1, "search_bid_rub": 50}],
        }
        try:
            r1 = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            r2 = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r1.json()["audit_id"] == r2.json()["audit_id"]
        finally:
            _reset_overrides()

    def test_same_key_same_apply_payload_cached_once(self):
        """Live apply: same key + same payload replays cached result with one network call."""
        settings = _settings("live_write")
        calls = {"keywordbids_set": 0, "keywords_get": 0}

        def set_handler(request: httpx.Request) -> httpx.Response:
            calls["keywordbids_set"] += 1
            return _build_keywordbids_set_handler(ok=True)(request)

        def get_handler(request: httpx.Request) -> httpx.Response:
            calls["keywords_get"] += 1
            return _build_keywords_get_handler()(request)

        multi = _build_multi_handler(set_handler, get_handler=get_handler)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, multi)
        )

        body = {
            "approved": True,
            "idempotency_key": "idem-live-replay-001",
            "dry_run": False,
            "items": [
                {"keyword_id": 1, "search_bid_rub": 250},
                {"keyword_id": 2, "context_bid_rub": 100},
            ],
        }
        try:
            r1 = client.post(f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body)
            r2 = client.post(f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body)
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r1.json()["audit_id"] == r2.json()["audit_id"]
            assert calls["keywordbids_set"] == 1
            assert calls["keywords_get"] == 1
        finally:
            _reset_overrides()

    def test_same_key_different_apply_payload_rejected(self):
        """Same key + same scope + different payload is rejected and not replayed."""
        settings = _settings("live_write")
        calls = {"keywordbids_set": 0}

        def set_handler(request: httpx.Request) -> httpx.Response:
            calls["keywordbids_set"] += 1
            return _build_keywordbids_set_handler(ok=True)(request)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, _build_multi_handler(set_handler))
        )

        body = {
            "approved": True,
            "idempotency_key": "idem-conflict-apply-001",
            "dry_run": False,
            "items": [{"keyword_id": 1, "search_bid_rub": 50}],
        }
        try:
            r1 = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert r1.status_code == 200

            body2 = {
                "approved": True,
                "idempotency_key": "idem-conflict-apply-001",
                "dry_run": False,
                "items": [{"keyword_id": 1, "search_bid_rub": 75}],
            }
            r2 = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body2
            )
            assert r2.status_code == 409
            assert r2.json()["detail"] == "Idempotency key 'idem-conflict-apply-001' was previously used with a different keyword bids payload; replay is rejected"
            _assert_no_token_in_error(r2)
            assert calls["keywordbids_set"] == 1
        finally:
            _reset_overrides()

    def test_same_key_different_dry_run_rejected(self):
        """Same key with different dry_run must fail."""
        # Use live_write with a mock transport so the endpoint gate passes
        # and we reach the store idempotency check.
        settings = _settings("live_write")
        calls = {"keywordbids_set": 0}

        def set_handler(request: httpx.Request) -> httpx.Response:
            calls["keywordbids_set"] += 1
            return _build_keywordbids_set_handler(ok=True)(request)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, _build_multi_handler(set_handler))
        )

        body_dry = {
            "approved": True,
            "idempotency_key": "idem-conflict-dry-001",
            "dry_run": True,
            "items": [{"keyword_id": 1, "search_bid_rub": 50}],
        }
        try:
            r1 = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body_dry
            )
            assert r1.status_code == 200

            body_apply = {
                "approved": True,
                "idempotency_key": "idem-conflict-dry-001",
                "dry_run": False,
                "items": [{"keyword_id": 1, "search_bid_rub": 75}],
            }
            r2 = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body_apply
            )
            assert r2.status_code == 409
            assert "previously used" in r2.json()["detail"]
            _assert_no_token_in_error(r2)
            assert calls["keywordbids_set"] == 0
        finally:
            _reset_overrides()


# ---------------------------------------------------------------------------
# TDD Step 5: Live apply + readback + provider warnings
# ---------------------------------------------------------------------------


def _build_keywordbids_set_handler(
    *,
    ok: bool = True,
    warnings: list[dict] | None = None,
    set_results: list[dict] | None = None,
):
    """Build a keywordbids.set handler returning the given envelope.

    ``set_results`` — per-item ``SetResults`` array for the v5 response.
    When ``None``, an empty ``SetResults: []`` is sent (current default).
    When provided, each item should be ``{\"Id\": int, ...}`` with optional
    ``Errors`` / ``Warnings`` arrays.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "set"
        assert "KeywordBids" in body["params"]
        items = body["params"]["KeywordBids"]
        assert len(items) > 0
        for item in items:
            assert "KeywordId" in item
            assert "CampaignId" not in item
            assert "AdGroupId" not in item

        result = {"SetResults": set_results if set_results is not None else []}
        error_block = None
        if not ok:
            error_block = {
                "error_code": 9300,
                "error_detail": "Превышено ограничение",
            }

        headers = {"Units": "42"}
        resp_body: dict[str, Any] = {
            "result": result,
            "Warnings": warnings or [],
        }
        if error_block is not None:
            resp_body["error"] = error_block
        return httpx.Response(
            200,
            json=resp_body,
            headers=headers,
            request=request,
        )

    return handler


def _build_keywords_get_handler(keywords: list[dict] | None = None):
    """Build a keywords.get handler for readback."""
    if keywords is None:
        keywords = [
            {"Id": 1, "Bid": 250000000, "ContextBid": 100000000},
            {"Id": 2, "Bid": 150000000, "ContextBid": 0},
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "get"
        return httpx.Response(
            200,
            json={"result": {"Keywords": keywords}},
            request=request,
        )

    return handler


def _build_multi_handler(
    set_handler,
    get_handler=None,
):
    """Build a handler that routes set vs get."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method", "")
        url = str(request.url)
        if "/keywordbids" in url and method == "set":
            return set_handler(request)
        if "/keywords" in url and method == "get":
            if get_handler:
                return get_handler(request)
            return _build_keywords_get_handler()(request)
        pytest.fail(f"Unexpected request: {url} {method}")
        return httpx.Response(500, json={})  # unreachable, satisfies type checker

    return handler


class TestLiveApply:
    """Test real apply in live_write mode."""

    def test_live_write_apply_success(self):
        """Successful apply returns applied=True with readback."""
        settings = _settings("live_write")
        multi = _build_multi_handler(_build_keywordbids_set_handler(ok=True))

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, multi)
        )

        body = {
            "approved": True,
            "idempotency_key": "live-apply-001",
            "dry_run": False,
            "items": [
                {"keyword_id": 1, "search_bid_rub": 250},
                {"keyword_id": 2, "context_bid_rub": 100},
            ],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            data = response.json()
            assert data["applied"] is True
            assert data["source"] == "yandex"
            assert data["dry_run"] is False
            assert data["yandex_units"] == 42

            readback = data.get("readback")
            assert readback is not None
            assert len(readback) == 2
            readback_ids = {kw["KeywordId"] for kw in readback}
            assert 1 in readback_ids
            assert 2 in readback_ids
            # Readback mirrors Yandex Direct keywords.get units: Bid/ContextBid are micros.
            first = next(kw for kw in readback if kw["KeywordId"] == 1)
            assert first["Bid"] == 250_000_000
            assert first["ContextBid"] == 100_000_000

            _assert_no_token_in_response(data)
        finally:
            _reset_overrides()

    def test_live_write_apply_with_provider_warnings(self):
        """Provider warnings (e.g. 10160) are surfaced in response."""
        settings = _settings("live_write")
        set_handler = _build_keywordbids_set_handler(
            ok=True,
            warnings=[
                {"Code": 10160, "Message": "Ставка не будет применена", "Details": ""}
            ],
        )
        multi = _build_multi_handler(set_handler)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, multi)
        )

        body = {
            "approved": True,
            "idempotency_key": "live-warn-001",
            "dry_run": False,
            "items": [{"keyword_id": 1, "search_bid_rub": 100}],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            data = response.json()
            assert data["applied"] is True
            warnings = data.get("provider_warnings", [])
            assert len(warnings) >= 1
            assert any(w["code"] == 10160 for w in warnings)
            _assert_no_token_in_response(data)
        finally:
            _reset_overrides()

    def test_live_write_apply_failure(self):
        """Top-level keywordbids.set failure maps to HTTP 502 with redacted detail."""
        settings = _settings("live_write")
        set_handler = _build_keywordbids_set_handler(ok=False)
        multi = _build_multi_handler(set_handler)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, multi)
        )

        body = {
            "approved": True,
            "idempotency_key": "live-fail-001",
            "dry_run": False,
            "items": [{"keyword_id": 1, "search_bid_rub": 100}],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 502
            detail = response.json()["detail"]
            assert detail["error_type"] == "YandexDirectError"
            assert detail["error_code"] == 9300
            assert "payload_preview" in detail
            _assert_no_token_in_error(response)
        finally:
            _reset_overrides()


# ---------------------------------------------------------------------------
# TDD Step 7: Per-item SetResults inspection (reviewer B1 fix)
# ---------------------------------------------------------------------------


class TestSetResults:
    """Verify per-item SetResults from keywordbids.set are surfaced."""

    def test_item_level_errors_cause_applied_false(self):
        """Per-item SetResults Errors cause applied=False + partial_failure=True."""
        settings = _settings("live_write")
        set_handler = _build_keywordbids_set_handler(
            ok=True,
            set_results=[
                {"Id": 1, "Errors": [{"Code": 52, "Message": "Ставка не задана", "Details": ""}]},
                {"Id": 2},
            ],
        )
        calls = {"set": 0, "get": 0}

        def counted_set(request: httpx.Request) -> httpx.Response:
            calls["set"] += 1
            return set_handler(request)

        def counted_get(request: httpx.Request) -> httpx.Response:
            calls["get"] += 1
            return _build_keywords_get_handler()(request)

        multi = _build_multi_handler(counted_set, get_handler=counted_get)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, multi)
        )

        body = {
            "approved": True,
            "idempotency_key": "sr-err-001",
            "dry_run": False,
            "items": [
                {"keyword_id": 1, "search_bid_rub": 250},
                {"keyword_id": 2, "search_bid_rub": 100},
            ],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            data = response.json()

            # Top-level: applied must be False because item 1 has errors
            assert data["applied"] is False
            assert data["partial_failure"] is True
            assert data["yandex_error"] is not None
            assert "item-level" in data["yandex_error"]
            assert data.get("readback") is None
            assert calls["set"] == 1
            assert calls["get"] == 0

            # set_results must be populated
            sr = data.get("set_results")
            assert sr is not None
            assert len(sr) == 2

            item1 = sr[0]
            assert item1["keyword_id"] == 1
            assert item1["has_errors"] is True
            assert len(item1["errors"]) == 1
            assert item1["errors"][0]["code"] == 52

            item2 = sr[1]
            assert item2["keyword_id"] == 2
            assert item2["has_errors"] is False

            _assert_no_token_in_response(data)
        finally:
            _reset_overrides()

    def test_item_level_warnings_surfaced(self):
        """Per-item SetResults Warnings are surfaced in provider_warnings + set_results."""
        settings = _settings("live_write")
        set_handler = _build_keywordbids_set_handler(
            ok=True,
            set_results=[
                {
                    "Id": 1,
                    "Warnings": [
                        {"Code": 10160, "Message": "Ставка не будет применена", "Details": ""}
                    ],
                },
            ],
        )
        multi = _build_multi_handler(set_handler)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, multi)
        )

        body = {
            "approved": True,
            "idempotency_key": "sr-warn-001",
            "dry_run": False,
            "items": [{"keyword_id": 1, "search_bid_rub": 100}],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            data = response.json()

            # No errors — applied must be True
            assert data["applied"] is True
            assert data["partial_failure"] is False

            # Item-level warning must appear in provider_warnings
            warnings = data.get("provider_warnings", [])
            assert any(w["code"] == 10160 for w in warnings)

            # set_results must show the warning item
            sr = data.get("set_results")
            assert sr is not None
            assert len(sr) == 1
            assert sr[0]["keyword_id"] == 1
            assert sr[0]["has_warnings"] is True
            assert sr[0]["has_errors"] is False
            assert len(sr[0]["warnings"]) == 1
            assert sr[0]["warnings"][0]["code"] == 10160

            _assert_no_token_in_response(data)
        finally:
            _reset_overrides()

    def test_successful_set_results_keep_pass(self):
        """Clean SetResults (no Errors, no Warnings) → applied=True."""
        settings = _settings("live_write")
        set_handler = _build_keywordbids_set_handler(
            ok=True,
            set_results=[
                {"Id": 1},
                {"Id": 2},
            ],
        )
        multi = _build_multi_handler(set_handler)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, multi)
        )

        body = {
            "approved": True,
            "idempotency_key": "sr-ok-001",
            "dry_run": False,
            "items": [
                {"keyword_id": 1, "search_bid_rub": 250},
                {"keyword_id": 2, "search_bid_rub": 100},
            ],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            data = response.json()

            assert data["applied"] is True
            assert data["partial_failure"] is False

            sr = data.get("set_results")
            assert sr is not None
            assert len(sr) == 2
            for item in sr:
                assert item["has_errors"] is False
                assert item["has_warnings"] is False

            _assert_no_token_in_response(data)
        finally:
            _reset_overrides()

    def test_no_set_results_envelope_still_applied_true(self):
        """Missing SetResults envelope (ok=True, no result.SetResults) → applied=True."""
        settings = _settings("live_write")

        def custom_multi(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            method = body.get("method", "")
            url = str(request.url)
            if "/keywordbids" in url and method == "set":
                return httpx.Response(
                    200,
                    json={"result": {}, "Warnings": []},
                    headers={"Units": "42"},
                    request=request,
                )
            if "/keywords" in url and method == "get":
                return _build_keywords_get_handler()(request)
            pytest.fail(f"Unexpected request: {url} {method}")
            return httpx.Response(500, json={})

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, custom_multi)
        )

        body = {
            "approved": True,
            "idempotency_key": "sr-noenv-001",
            "dry_run": False,
            "items": [{"keyword_id": 1, "search_bid_rub": 100}],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            data = response.json()
            assert data["applied"] is True
            assert data["partial_failure"] is False
            # set_results should be None when no SetResults in envelope
            assert data.get("set_results") is None
            _assert_no_token_in_response(data)
        finally:
            _reset_overrides()

    def test_no_token_leakage_in_set_results(self):
        """SECRET_TOKEN never appears in set_results or error details."""
        settings = _settings("live_write")
        set_handler = _build_keywordbids_set_handler(
            ok=True,
            set_results=[
                {"Id": 1, "Errors": [{"Code": 52, "Message": "error", "Details": ""}]},
            ],
        )
        multi = _build_multi_handler(set_handler)

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_yandex_client] = (
            lambda: _client_with_handler(settings, multi)
        )

        body = {
            "approved": True,
            "idempotency_key": "sr-leak-001",
            "dry_run": False,
            "items": [{"keyword_id": 1, "search_bid_rub": 100}],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            data = response.json()
            _assert_no_token_in_response(data)

            # Additionally verify set_results does not contain raw payload
            sr = data.get("set_results", [])
            for item in sr or []:
                dumped = json.dumps(item, default=str)
                assert SECRET_TOKEN not in dumped
                # Raw v5 fields (Code, Message, Details are fine; raw body fields are not)
                assert "error_code" not in dumped  # should be redacted to code/message/details
        finally:
            _reset_overrides()


# ---------------------------------------------------------------------------
# TDD Step 6: No token leakage
# ---------------------------------------------------------------------------


class TestNoTokenLeakage:
    """Verify no secret tokens appear in responses."""

    def test_dry_run_response_no_token(self):
        app.dependency_overrides[get_settings] = lambda: _settings("live_readonly")

        body = {
            "approved": True,
            "idempotency_key": "no-leak-001",
            "dry_run": True,
            "items": [{"keyword_id": 1, "search_bid_rub": 100}],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 200
            _assert_no_token_in_response(response.json())
        finally:
            _reset_overrides()

    def test_error_response_no_token(self):
        app.dependency_overrides[get_settings] = lambda: _settings("live_readonly")

        body = {
            "approved": True,
            "idempotency_key": "no-leak-002",
            "dry_run": False,
            "items": [{"keyword_id": 1, "search_bid_rub": 100}],
        }
        try:
            response = client.post(
                f"/yandex/campaigns/{MOCK_CAMPAIGN_ID}/bids", json=body
            )
            assert response.status_code == 409
            _assert_no_token_in_error(response)
        finally:
            _reset_overrides()
