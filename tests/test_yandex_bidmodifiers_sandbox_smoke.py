from __future__ import annotations

import json
import os
from typing import Any
from urllib import error, request

import pytest


pytestmark = pytest.mark.live_smoke


SANDBOX_BASE = "https://api-sandbox.direct.yandex.com/json/v5"


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _call(token: str, service: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode()
    req = request.Request(
        f"{SANDBOX_BASE}/{service}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return {"http": resp.status, "units": resp.headers.get("Units"), "payload": payload}
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"non_json_prefix": text[:500]}
        return {"http": exc.code, "units": exc.headers.get("Units"), "payload": payload}


def _top_error(response: dict[str, Any]) -> dict[str, Any] | None:
    payload = response.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        err = payload["error"]
        return {
            "error_code": err.get("error_code"),
            "error_string": err.get("error_string"),
            "error_detail": err.get("error_detail"),
        }
    return None


def _result_list(response: dict[str, Any], name: str) -> list[dict[str, Any]]:
    payload = response.get("payload") or {}
    result = payload.get("result") if isinstance(payload, dict) else None
    items = result.get(name) if isinstance(result, dict) else None
    return items if isinstance(items, list) else []


def _item_errors(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        raw = item.get("Errors") if isinstance(item, dict) else None
        if isinstance(raw, list) and raw:
            errors.append({"index": index, "errors": raw})
    return errors


def _extract_bid_modifier(value: Any) -> int | None:
    if isinstance(value, dict):
        raw = value.get("BidModifier")
        if isinstance(raw, int):
            return raw
        for nested in value.values():
            found = _extract_bid_modifier(nested)
            if found is not None:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _extract_bid_modifier(nested)
            if found is not None:
                return found
    return None


def _coerce_id(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _get_params(selection_criteria: dict[str, Any]) -> dict[str, Any]:
    selection = dict(selection_criteria)
    selection.setdefault("Levels", ["CAMPAIGN", "AD_GROUP"])
    return {
        "SelectionCriteria": selection,
        "FieldNames": ["Id", "CampaignId", "AdGroupId", "Level", "Type"],
        "MobileAdjustmentFieldNames": ["BidModifier", "OperatingSystemType"],
        "TabletAdjustmentFieldNames": ["BidModifier", "OperatingSystemType"],
        "DesktopAdjustmentFieldNames": ["BidModifier"],
        "DesktopOnlyAdjustmentFieldNames": ["BidModifier"],
        "DemographicsAdjustmentFieldNames": ["Gender", "Age", "BidModifier", "Enabled"],
        "RetargetingAdjustmentFieldNames": [
            "RetargetingConditionId",
            "BidModifier",
            "Accessible",
            "Enabled",
        ],
        "RegionalAdjustmentFieldNames": ["RegionId", "BidModifier", "Enabled"],
        "VideoAdjustmentFieldNames": ["BidModifier"],
        "SmartAdAdjustmentFieldNames": ["BidModifier"],
        "SerpLayoutAdjustmentFieldNames": ["SerpLayout", "BidModifier", "Enabled"],
        "IncomeGradeAdjustmentFieldNames": ["Grade", "BidModifier", "Enabled"],
        "AdGroupAdjustmentFieldNames": ["BidModifier"],
    }


def _find_existing_modifier(
    token: str,
    campaign_id: int | str,
    modifier_type: str | None,
) -> dict[str, Any]:
    response = _call(
        token,
        "bidmodifiers",
        "get",
        _get_params({"CampaignIds": [campaign_id]}),
    )
    assert response["http"] == 200
    assert _top_error(response) is None
    rows = _result_list(response, "BidModifiers")
    for row in rows:
        if modifier_type and row.get("Type") != modifier_type:
            continue
        if isinstance(row.get("Id"), int) and _extract_bid_modifier(row) is not None:
            return row
    type_suffix = f" of type {modifier_type}" if modifier_type else ""
    pytest.skip(f"sandbox campaign has no existing bid modifier{type_suffix}")
    raise AssertionError("unreachable after pytest.skip")


def test_sandbox_bidmodifiers_set_existing_modifier_noop_smoke() -> None:
    """Opt-in real sandbox smoke for Direct ``bidmodifiers.set``.

    This test validates the provider shape for an existing modifier discovered
    from ``bidmodifiers.get``:
    ``{"BidModifiers": [{"Id": modifier_id, "BidModifier": current_value}]}``.
    It is skipped by default and performs a no-op value write unless the caller
    explicitly enables sandbox smoke. If ``YANDEX_DIRECT_SANDBOX_MODIFIER_ID``
    is not provided, the test discovers one from ``YANDEX_DIRECT_SANDBOX_CAMPAIGN_ID``
    and optional ``YANDEX_DIRECT_SANDBOX_MODIFIER_TYPE``.

    Required env:
    - ``DIRECTPILOT_YANDEX_SANDBOX_SMOKE=bidmodifiers_set``
    - ``YANDEX_DIRECT_SANDBOX_TOKEN``
    - either ``YANDEX_DIRECT_SANDBOX_MODIFIER_ID`` or ``YANDEX_DIRECT_SANDBOX_CAMPAIGN_ID``
    Optional env:
    - ``YANDEX_DIRECT_SANDBOX_MODIFIER_TYPE`` (for example ``MOBILE_ADJUSTMENT``)
    """

    if _env("DIRECTPILOT_YANDEX_SANDBOX_SMOKE") != "bidmodifiers_set":
        pytest.skip("set DIRECTPILOT_YANDEX_SANDBOX_SMOKE=bidmodifiers_set to run")
    token = _env("YANDEX_DIRECT_SANDBOX_TOKEN")
    modifier_id_raw = _env("YANDEX_DIRECT_SANDBOX_MODIFIER_ID")
    campaign_id_raw = _env("YANDEX_DIRECT_SANDBOX_CAMPAIGN_ID")
    modifier_type = _env("YANDEX_DIRECT_SANDBOX_MODIFIER_TYPE")
    if not token:
        pytest.skip("sandbox token is required")
    assert token is not None

    if modifier_id_raw:
        modifier_id = _coerce_id(modifier_id_raw)
        pre = _call(token, "bidmodifiers", "get", _get_params({"Ids": [modifier_id]}))
        assert pre["http"] == 200
        assert _top_error(pre) is None
        rows = _result_list(pre, "BidModifiers")
        row = next((item for item in rows if item.get("Id") == modifier_id), None)
        assert row is not None, "existing sandbox bid modifier was not found by Id"
    else:
        if not campaign_id_raw:
            pytest.skip("sandbox modifier id or campaign id is required")
        assert campaign_id_raw is not None
        campaign_id = _coerce_id(campaign_id_raw)
        row = _find_existing_modifier(token, campaign_id, modifier_type)
        modifier_id = row["Id"]

    assert isinstance(modifier_id, int)
    current = _extract_bid_modifier(row)
    assert current is not None, "sandbox bid modifier has no readable BidModifier"

    set_response = _call(
        token,
        "bidmodifiers",
        "set",
        {"BidModifiers": [{"Id": modifier_id, "BidModifier": current}]},
    )
    assert set_response["http"] == 200
    assert _top_error(set_response) is None
    set_results = _result_list(set_response, "SetResults")
    assert _item_errors(set_results) == []
    assert set_results and set_results[0].get("Id") == modifier_id

    readback = _call(token, "bidmodifiers", "get", _get_params({"Ids": [modifier_id]}))
    assert readback["http"] == 200
    assert _top_error(readback) is None
    rows_after = _result_list(readback, "BidModifiers")
    row_after = next((item for item in rows_after if item.get("Id") == modifier_id), None)
    assert row_after is not None
    assert _extract_bid_modifier(row_after) == current
