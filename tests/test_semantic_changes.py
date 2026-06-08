"""Tests for the staged semantic-change package endpoint.

This endpoint lets the user prepare a semantic change (add/replace
negative keywords and/or add/replace positive keywords for a real
Yandex Direct campaign, e.g. ``710382063``) and then explicitly apply
it. The apply path is fully gated:

* ``dry_run=True`` is ALWAYS allowed and NEVER performs a network
  write. The result is a preview of the Direct API v5 ``keywords.add``
  / ``adgroups.update`` request bodies that WOULD be sent.
* In ``live_readonly`` mode, ``dry_run=False`` (i.e. real apply) is
  REJECTED before any network call. The response must mention
  ``live_readonly`` and must NOT echo the OAUTH token.
* In ``live_write`` mode, ``dry_run=False`` is allowed only when
  ``approved=True`` and an ``idempotency_key`` is supplied. Without
  either of those, the request is rejected.
* Audit events are recorded for both prepare and apply. They never
  contain the OAUTH token.
* Both keyword operations require an explicit ``ad_group_id`` on the
  request — Direct API v5 ``keywords.add`` needs ``AdGroupId`` per
  keyword and ``adgroups.update`` needs the target group ``Id`` for
  ``NegativeKeywords.Items``. Endpoints reject requests missing it
  with HTTP 400.

The tests below pin this contract. We do NOT exercise any real Yandex
write call — the request body that WOULD be sent is what we inspect,
and the live-write success path is verified via httpx.MockTransport so
it stays hermetic.
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


def _settings(mode: str, token: str | None = "t-test-token") -> Settings:
    return Settings(_env_file=None, directpilot_mode=mode, yandex_oauth_token=token)


def _client_with_handler(
    settings: Settings, handler
) -> YandexDirectClient:
    """Build a YandexDirectClient whose transport is the given httpx handler.

    The handler is wrapped so we can assert that no real network call
    happened on the rejected / dry-run paths.
    """

    def _transport(request: httpx.Request) -> httpx.Response:
        try:
            return handler(request)
        except AssertionError:
            raise

    transport = httpx.MockTransport(_transport)
    return YandexDirectClient(settings=settings, transport=transport)


def _semantic_payload(
    *,
    add_negative: list[str] | None = None,
    add_keywords: list[str] | None = None,
    ad_group_id: int | str | None = 123456789,
) -> dict[str, Any]:
    body: dict[str, Any] = {"campaign_id": "710382063"}
    if add_negative is not None:
        body["add_negative_keywords"] = add_negative
    if add_keywords is not None:
        body["add_keywords"] = add_keywords
    if ad_group_id is not None:
        body["ad_group_id"] = ad_group_id
    return body


def _capture_handler(captured: dict[str, Any]) -> Any:
    """Build a mock transport handler that records the request envelope."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.setdefault("urls", []).append(str(request.url))
        captured.setdefault("bodies", []).append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 1}]}})

    return handler


# ---------------------------------------------------------------------------
# prepare: dry-run path always allowed
# ---------------------------------------------------------------------------


def test_prepare_semantic_change_in_live_readonly_is_allowed_and_never_calls_yandex():
    """In live_readonly we can still PREPARE (build the request body).

    Prepare is pure-local: it does not call Yandex and it does not
    require approval. The user gets back a package id and a preview of
    the Direct API v5 payload that would be sent on apply.
    """
    settings = _settings("live_readonly", token="TOPSECRET-PREPARE")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called on prepare")

    # Build a client whose transport MUST NOT be hit on prepare; the
    # whole point of this test is to assert the network is silent.
    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            "/campaigns/710382063/semantic-changes",
            json=_semantic_payload(
                add_negative=["как поменять тэн", "своими руками"],
                add_keywords=["ремонт посудомоечных машин казань"],
                ad_group_id=987654321,
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign_id"] == "710382063"
    assert body["status"] == "prepared"
    assert "package_id" in body and body["package_id"]
    assert body["dry_run"] is True
    # The preview must mention the methods that would be called.
    methods = {op["method"] for op in body["preview"]["operations"]}
    assert "keywords.add" in methods
    assert "adgroups.update" in methods
    # merge_on_apply is True whenever the user supplied
    # add_negative_keywords, so the live apply path can warn the user
    # about the read-modify-write before they approve.
    assert body["preview"]["merge_on_apply"] is True
    assert body["preview"]["semantics_note"]
    assert "REPLACE" in body["preview"]["semantics_note"]
    # No token in the response.
    assert "TOPSECRET-PREPARE" not in response.text


def test_prepare_merge_on_apply_is_false_when_only_positive_keywords():
    """merge_on_apply is a no-op when the package has no negative
    keywords — Direct's `keywords.add` is APPEND by design, so the
    only operation that needs the read-modify-write is the negative
    one.
    """
    response = client.post(
        "/campaigns/710382063/semantic-changes",
        json=_semantic_payload(
            add_keywords=["ремонт стиральных машин казань"],
            ad_group_id=123456789,
        ),
    )
    assert response.status_code == 200
    preview = response.json()["preview"]
    assert preview["merge_on_apply"] is False
    assert preview["semantics_note"] is None


def test_prepare_includes_negative_and_positive_operations():
    response = client.post(
        "/campaigns/710382063/semantic-changes",
        json=_semantic_payload(
            add_negative=["как заменить насос"],
            add_keywords=["замена тэна посудомоечной машины bosch"],
            ad_group_id=111222333,
        ),
    )
    assert response.status_code == 200
    body = response.json()
    operations = body["preview"]["operations"]
    # We expect at least one negative-keyword op and at least one
    # positive-keyword op to be present in the preview.
    flatten = json.dumps(operations, ensure_ascii=False)
    assert "как заменить насос" in flatten
    assert "замена тэна посудомоечной машины bosch" in flatten


def test_prepare_preview_uses_confirmed_v5_payloads():
    """The preview MUST match the documented v5 contract.

    * ``add_keywords`` -> one ``keywords.add`` op with
      ``{"Keywords": [{"Keyword": phrase, "AdGroupId": <int>}, ...]}``.
    * ``add_negative_keywords`` -> one ``adgroups.update`` op with
      ``{"AdGroups": [{"Id": <int>, "NegativeKeywords": {"Items":
      [<phrase>, ...]}}]}``. Leading ``-`` is stripped.
    """
    response = client.post(
        "/campaigns/710382063/semantic-changes",
        json=_semantic_payload(
            add_negative=["-бесплатно", "  своими руками  "],
            add_keywords=["ремонт стиральных машин казань"],
            ad_group_id=123456789,
        ),
    )
    assert response.status_code == 200
    operations = response.json()["preview"]["operations"]

    keywords_op = next(op for op in operations if op["method"] == "keywords.add")
    assert keywords_op["params"] == {
        "Keywords": [
            {"Keyword": "ремонт стиральных машин казань", "AdGroupId": 123456789},
        ],
    }
    # service/method shape: there is no explicit "service" key on the
    # preview envelope — the store routes by method name when applying.

    neg_op = next(op for op in operations if op["method"] == "adgroups.update")
    assert neg_op["params"] == {
        "AdGroups": [
            {
                "Id": 123456789,
                "NegativeKeywords": {"Items": ["бесплатно", "своими руками"]},
            }
        ],
    }
    # Confirm there is no CampaignId field anywhere — Direct's
    # adgroups.update resolves the campaign from the AdGroupId.
    assert "CampaignId" not in json.dumps(neg_op, ensure_ascii=False)


def test_prepare_rejects_missing_ad_group_id_for_add_keywords():
    """add_keywords without ad_group_id must return HTTP 400, no package built."""
    response = client.post(
        "/campaigns/710382063/semantic-changes",
        json={
            "add_keywords": ["ремонт стиральных машин казань"],
        },
    )
    assert response.status_code == 400, response.text
    assert "ad_group_id" in response.text


def test_prepare_rejects_missing_ad_group_id_for_add_negative_keywords():
    """add_negative_keywords without ad_group_id must also return HTTP 400."""
    response = client.post(
        "/campaigns/710382063/semantic-changes",
        json={
            "add_negative_keywords": ["своими руками"],
        },
    )
    assert response.status_code == 400, response.text
    assert "ad_group_id" in response.text


def test_prepare_accepts_string_ad_group_id_and_normalises_to_int_in_preview():
    response = client.post(
        "/campaigns/710382063/semantic-changes",
        json=_semantic_payload(
            add_keywords=["ремонт стиральных машин казань"],
            ad_group_id="123456789",
        ),
    )
    assert response.status_code == 200, response.text
    op = response.json()["preview"]["operations"][0]
    assert op["method"] == "keywords.add"
    assert op["params"]["Keywords"][0]["AdGroupId"] == 123456789


# ---------------------------------------------------------------------------
# apply gates
# ---------------------------------------------------------------------------


def test_apply_in_live_readonly_is_blocked_before_network_call():
    """In live_readonly, real apply is REJECTED before any network call."""
    settings = _settings("live_readonly", token="TOPSECRET-BLOCK")

    network_called = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        network_called["calls"] += 1
        return httpx.Response(200, json={"result": [{}]})

    yandex = _client_with_handler(settings, handler)

    # Prepare the package first (in mock mode, so we have a stable id).
    prepare = client.post(
        "/campaigns/710382063/semantic-changes",
        json=_semantic_payload(
            add_negative=["как поменять тэн"], ad_group_id=111
        ),
    )
    assert prepare.status_code == 200
    package_id = prepare.json()["package_id"]

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            f"/semantic-changes/{package_id}/apply",
            json={
                "dry_run": False,
                "approved": True,
                "idempotency_key": "smoke-block-001",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "live_readonly" in str(detail)
    assert "TOPSECRET-BLOCK" not in response.text
    assert network_called["calls"] == 0


def test_apply_dry_run_is_always_allowed_and_marks_dry_run_true():
    """dry_run=True on apply never calls Yandex and reports applied=False."""
    settings = _settings("live_write", token="TOPSECRET-DR")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called on dry_run apply")

    yandex = _client_with_handler(settings, handler)

    prepare = client.post(
        "/campaigns/710382063/semantic-changes",
        json=_semantic_payload(
            add_negative=["своими руками"], ad_group_id=111
        ),
    )
    package_id = prepare.json()["package_id"]

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        response = client.post(
            f"/semantic-changes/{package_id}/apply",
            json={
                "dry_run": True,
                "approved": True,
                "idempotency_key": "smoke-dryrun-001",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["mode"] in {"sandbox", "live_readonly", "live_write"}
    assert "TOPSECRET-DR" not in response.text


def test_apply_live_write_without_approved_is_rejected():
    settings = _settings("live_write", token="TOPSECRET-NOAPP")
    prepare = client.post(
        "/campaigns/710382063/semantic-changes",
        json=_semantic_payload(
            add_negative=["своими руками"], ad_group_id=111
        ),
    )
    package_id = prepare.json()["package_id"]

    response = client.post(
        f"/semantic-changes/{package_id}/apply",
        json={
            "dry_run": False,
            "approved": False,
            "idempotency_key": "smoke-noapprove-001",
        },
    )
    assert response.status_code == 409, response.text
    assert "approval" in response.text.lower() or "approved" in response.text.lower()
    assert "TOPSECRET-NOAPP" not in response.text


def test_apply_live_write_with_full_gates_calls_confirmed_helpers_and_marks_applied():
    """live_write + approved + idempotency_key -> the store must dispatch
    to ``YandexDirectClient.keywords_add`` and
    ``YandexDirectClient.adgroups_update`` over the v5 ``keywords`` and
    ``adgroups`` services and return ``applied=True``.

    The mock transport asserts the exact request envelope so we never
    regress to a fake legacy update shape instead of the confirmed v5 add/update shapes.

    For a package with negative keywords, the live apply path performs
    a read-modify-write: one ``adgroups.get`` (to read existing
    negatives) followed by one ``adgroups.update`` (with the merged
    list). Combined with ``keywords.add`` that is THREE HTTP requests
    in total.
    """
    settings = _settings("live_write", token="TOPSECRET-FULL")
    captured: dict[str, Any] = {
        "calls": 0,
        "urls": [],
        "bodies": [],
        "bodies_by_url": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        url = str(request.url)
        body = json.loads(request.content.decode())
        captured["urls"].append(url)
        captured["bodies"].append(body)
        captured["bodies_by_url"].setdefault(url, []).append(body)
        if url.endswith("/adgroups") and body.get("method") == "get":
            # Return a fake ad groups envelope that contains the
            # target ad group id with one pre-existing negative. The
            # merge must keep that phrase in the final payload.
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AdGroups": [
                            {
                                "Id": 123456789,
                                "Name": "тест",
                                "NegativeKeywords": {"Items": ["уже было"]},
                            }
                        ]
                    }
                },
            )
        if url.endswith("/adgroups") and body.get("method") == "update":
            return httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 1}]}})
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 1}]}})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        prepare = client.post(
            "/campaigns/710382063/semantic-changes",
            json=_semantic_payload(
                add_negative=["своими руками", "-бесплатно"],
                add_keywords=["ремонт стиральных машин казань"],
                ad_group_id=123456789,
            ),
        )
        package_id = prepare.json()["package_id"]

        response = client.post(
            f"/semantic-changes/{package_id}/apply",
            json={
                "dry_run": False,
                "approved": True,
                "idempotency_key": "smoke-full-001",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is False
    assert body["applied"] is True
    assert body["mode"] == "live_write"
    assert body["source"] == "yandex"
    assert body["operations_sent"] == 2
    # No token leakage.
    assert "TOPSECRET-FULL" not in response.text

    # THREE HTTP requests: adgroups.get (read), adgroups.update (write),
    # keywords.add (positive op). The order is implementation-defined
    # (operations are processed in package order) but the SET of
    # (url, method) tuples is stable.
    assert captured["calls"] == 3
    unique_urls = sorted(set(captured["urls"]))
    assert unique_urls == [
        "https://api.direct.yandex.com/json/v5/adgroups",
        "https://api.direct.yandex.com/json/v5/keywords",
    ]

    by_url_method = [
        (b.get("method"), b) for b in captured["bodies"]
    ]
    # Sanity: exactly one GET, one UPDATE, one ADD.
    method_counts: dict[str, int] = {}
    for method, _ in by_url_method:
        if method is None:
            continue
        method_counts[method] = method_counts.get(method, 0) + 1
    assert method_counts.get("get") == 1
    assert method_counts.get("update") == 1
    assert method_counts.get("add") == 1

    # Confirm payloads match the v5 contract.
    # keywords.add
    keywords_call = next(b for m, b in by_url_method if m == "add")
    assert keywords_call == {
        "method": "add",
        "params": {
            "Keywords": [
                {"Keyword": "ремонт стиральных машин казань", "AdGroupId": 123456789},
            ],
        },
    }
    # adgroups.update — must contain the MERGED negative list
    # (pre-existing "уже было" PLUS the requested phrases, order
    # preserved, normalised without leading "-").
    adgroups_update_call = next(b for m, b in by_url_method if m == "update")
    assert adgroups_update_call == {
        "method": "update",
        "params": {
            "AdGroups": [
                {
                    "Id": 123456789,
                    "NegativeKeywords": {
                        "Items": ["уже было", "своими руками", "бесплатно"],
                    },
                }
            ],
        },
    }
    # adgroups.get — FieldNames must include "NegativeKeywords" so the
    # merge path has data to read.
    adgroups_get_call = next(b for m, b in by_url_method if m == "get")
    assert "NegativeKeywords" in adgroups_get_call["params"]["FieldNames"]

    # Audit event recorded with applied + units + merge_on_apply, no
    # token.
    applied_events = [
        event
        for event in store.audit_events
        if event.action == "semantic_change_applied"
    ]
    assert applied_events, "expected semantic_change_applied audit event"
    last = applied_events[-1]
    assert last.details.get("merge_on_apply") is True
    assert "TOPSECRET-FULL" not in json.dumps(last.details, ensure_ascii=False)


def test_apply_live_write_only_positive_keywords_sends_one_keywords_add_call():
    settings = _settings("live_write", token="TOPSECRET-POS")
    captured: dict[str, Any] = {"calls": 0, "urls": [], "bodies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        captured["urls"].append(str(request.url))
        captured["bodies"].append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 1}]}})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        prepare = client.post(
            "/campaigns/710382063/semantic-changes",
            json=_semantic_payload(
                add_keywords=["ремонт стиральных машин казань"],
                ad_group_id=42,
            ),
        )
        package_id = prepare.json()["package_id"]
        response = client.post(
            f"/semantic-changes/{package_id}/apply",
            json={
                "dry_run": False,
                "approved": True,
                "idempotency_key": "smoke-pos-001",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured["calls"] == 1
    assert captured["urls"] == ["https://api.direct.yandex.com/json/v5/keywords"]
    assert captured["bodies"][0]["params"]["Keywords"][0]["AdGroupId"] == 42


def test_apply_live_write_only_negative_keywords_sends_read_then_adgroups_update_call():
    settings = _settings("live_write", token="TOPSECRET-NEG")
    captured: dict[str, Any] = {"calls": 0, "urls": [], "bodies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        captured["urls"].append(str(request.url))
        body = json.loads(request.content.decode())
        captured["bodies"].append(body)
        if body.get("method") == "get":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "AdGroups": [
                            {
                                "Id": 99,
                                "Name": "test",
                                "NegativeKeywords": {"Items": ["уже было"]},
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 1}]}})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        prepare = client.post(
            "/campaigns/710382063/semantic-changes",
            json=_semantic_payload(
                add_negative=["своими руками"],
                ad_group_id=99,
            ),
        )
        package_id = prepare.json()["package_id"]
        response = client.post(
            f"/semantic-changes/{package_id}/apply",
            json={
                "dry_run": False,
                "approved": True,
                "idempotency_key": "smoke-neg-001",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured["calls"] == 2
    assert captured["urls"] == [
        "https://api.direct.yandex.com/json/v5/adgroups",
        "https://api.direct.yandex.com/json/v5/adgroups",
    ]
    get_body, update_body = captured["bodies"]
    assert get_body["method"] == "get"
    assert get_body["params"]["SelectionCriteria"]["CampaignIds"] == [710382063]
    assert "NegativeKeywords" in get_body["params"]["FieldNames"]
    assert update_body["method"] == "update"
    assert update_body["params"]["AdGroups"][0]["Id"] == 99
    assert update_body["params"]["AdGroups"][0]["NegativeKeywords"]["Items"] == [
        "уже было",
        "своими руками",
    ]


def test_apply_live_write_records_semantic_change_apply_failed_on_yandex_error():
    """If Yandex rejects an operation, audit ``semantic_change_apply_failed``
    and re-raise so the endpoint returns 502. No token leakage."""
    settings = _settings("live_write", token="TOPSECRET-FAIL")
    captured: dict[str, Any] = {"calls": 0, "urls": [], "bodies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        captured["urls"].append(str(request.url))
        captured["bodies"].append(json.loads(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "error": {"error_code": 500, "message": "boom"},
            },
        )

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        prepare = client.post(
            "/campaigns/710382063/semantic-changes",
            json=_semantic_payload(
                add_keywords=["ремонт стиральных машин казань"],
                ad_group_id=1,
            ),
        )
        package_id = prepare.json()["package_id"]
        response = client.post(
            f"/semantic-changes/{package_id}/apply",
            json={
                "dry_run": False,
                "approved": True,
                "idempotency_key": "smoke-fail-001",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502, response.text
    assert "TOPSECRET-FAIL" not in response.text
    failed_events = [
        e
        for e in store.audit_events
        if e.action == "semantic_change_apply_failed"
    ]
    assert failed_events, "expected semantic_change_apply_failed audit event"
    last = failed_events[-1]
    assert "TOPSECRET-FAIL" not in json.dumps(last.details, ensure_ascii=False)


def test_apply_idempotency_key_avoids_duplicate_dry_run_results():
    settings = _settings("live_write", token="TOPSECRET-IDEM")
    captured: dict[str, Any] = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        return httpx.Response(200, json={"result": [{"Id": 1}]})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        prepare = client.post(
            "/campaigns/710382063/semantic-changes",
            json=_semantic_payload(
                add_negative=["своими руками"], ad_group_id=1
            ),
        )
        package_id = prepare.json()["package_id"]
        payload = {
            "dry_run": True,
            "approved": True,
            "idempotency_key": "smoke-idem-001",
        }
        r1 = client.post(f"/semantic-changes/{package_id}/apply", json=payload)
        r2 = client.post(f"/semantic-changes/{package_id}/apply", json=payload)
    finally:
        app.dependency_overrides.clear()

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["audit_id"] == r2.json()["audit_id"]
    assert captured["calls"] == 0


def test_apply_live_write_idempotency_key_avoids_duplicate_live_calls():
    """Replays of the same live-write apply MUST NOT re-send to Yandex."""
    settings = _settings("live_write", token="TOPSECRET-IDEM-LIVE")
    captured: dict[str, Any] = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 1}]}})

    yandex = _client_with_handler(settings, handler)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_yandex_client] = lambda: yandex
    try:
        prepare = client.post(
            "/campaigns/710382063/semantic-changes",
            json=_semantic_payload(
                add_keywords=["ремонт стиральных машин казань"],
                ad_group_id=1,
            ),
        )
        package_id = prepare.json()["package_id"]
        body = {
            "dry_run": False,
            "approved": True,
            "idempotency_key": "smoke-idem-live-001",
        }
        r1 = client.post(f"/semantic-changes/{package_id}/apply", json=body)
        r2 = client.post(f"/semantic-changes/{package_id}/apply", json=body)
    finally:
        app.dependency_overrides.clear()

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["audit_id"] == r2.json()["audit_id"]
    # Only the first call hits the network.
    assert captured["calls"] == 1


# ---------------------------------------------------------------------------
# YandexDirectClient helpers
# ---------------------------------------------------------------------------


def test_yandex_direct_client_keywords_add_posts_to_keywords_service_with_method_add():
    settings = _settings("live_write", token="SECRET-HELPER")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"AddResults": [{"Id": 7}]}})

    client_obj = YandexDirectClient(
        settings=settings, transport=httpx.MockTransport(handler)
    )
    result = client_obj.keywords_add(
        [{"Keyword": "ремонт казань", "AdGroupId": 123456789}]
    )
    assert result["ok"] is True
    assert captured["url"] == "https://api.direct.yandex.com/json/v5/keywords"
    assert captured["body"] == {
        "method": "add",
        "params": {
            "Keywords": [{"Keyword": "ремонт казань", "AdGroupId": 123456789}],
        },
    }
    assert "SECRET-HELPER" not in str(result)


def test_yandex_direct_client_adgroups_update_posts_to_adgroups_service_with_method_update():
    settings = _settings("live_write", token="SECRET-HELPER-2")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 8}]}})

    client_obj = YandexDirectClient(
        settings=settings, transport=httpx.MockTransport(handler)
    )
    result = client_obj.adgroups_update(
        [
            {
                "Id": 123456789,
                "NegativeKeywords": {"Items": ["бесплатно"]},
            }
        ]
    )
    assert result["ok"] is True
    assert captured["url"] == "https://api.direct.yandex.com/json/v5/adgroups"
    assert captured["body"] == {
        "method": "update",
        "params": {
            "AdGroups": [
                {"Id": 123456789, "NegativeKeywords": {"Items": ["бесплатно"]}}
            ],
        },
    }
    assert "SECRET-HELPER-2" not in str(result)


# ---------------------------------------------------------------------------
# openapi / docs smoke
# ---------------------------------------------------------------------------


def test_openapi_documents_semantic_change_endpoints():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    paths = spec["paths"]
    assert "/campaigns/{campaign_id}/semantic-changes" in paths
    assert "/semantic-changes/{package_id}/apply" in paths


# ---------------------------------------------------------------------------
# fixture cleanup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_store():
    """Reset the in-memory store between tests so audit counts are stable.

    The semantic-change attributes are added in GREEN, so we use
    ``getattr(..., default)`` to stay safe during the RED phase and on
    tests that don't touch them.
    """
    store.audit_events.clear()
    for attr in ("semantic_packages_by_id", "semantic_apply_results_by_key"):
        if hasattr(store, attr):
            getattr(store, attr).clear()
    yield
    store.audit_events.clear()
    for attr in ("semantic_packages_by_id", "semantic_apply_results_by_key"):
        if hasattr(store, attr):
            getattr(store, attr).clear()
