from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import BidModifierAdjustment, BidModifierAgeAdjustment, BidModifiersUpdateRequest


def test_bid_modifier_under_18_minus_100_builds_safe_preview_for_campaign():
    request = BidModifiersUpdateRequest(
        approved=False,
        idempotency_key="preview-123",
        adjustments=[BidModifierAgeAdjustment(adjustment_percent=-100)],
    )

    preview = request.build_payload_preview(710691939)

    assert preview == {
        "BidModifiers": [
            {
                "CampaignId": 710691939,
                "AgeRange": "AGE_0_17",
                "AdjustmentPercent": -100,
                "BidModifier": 0,
            }
        ]
    }


def test_bid_modifier_under_18_minus_100_builds_direct_set_payload_with_existing_id():
    request = BidModifiersUpdateRequest(
        approved=True,
        dry_run=False,
        idempotency_key="apply-123",
        adjustments=[BidModifierAgeAdjustment(modifier_id=987654, adjustment_percent=-100)],
    )

    assert request.build_direct_set_payload() == {
        "BidModifiers": [{"Id": 987654, "BidModifier": 0}]
    }


def test_bid_modifier_direct_set_payload_requires_existing_modifier_id():
    request = BidModifiersUpdateRequest(
        approved=True,
        dry_run=False,
        idempotency_key="apply-123",
        adjustments=[BidModifierAgeAdjustment(adjustment_percent=-100)],
    )

    with pytest.raises(ValueError, match="modifier_id is required"):
        request.build_direct_set_payload()


@pytest.mark.parametrize("value", [-101, 1201])
def test_bid_modifier_adjustment_percent_out_of_range_rejected(value: int):
    with pytest.raises(ValidationError):
        BidModifierAgeAdjustment(adjustment_percent=value)


def test_bid_modifiers_request_rejects_empty_adjustments():
    with pytest.raises(ValidationError):
        BidModifiersUpdateRequest(
            approved=False,
            idempotency_key="preview-123",
            adjustments=[],
        )


def test_bid_modifiers_request_rejects_short_idempotency_key():
    with pytest.raises(ValidationError):
        BidModifiersUpdateRequest(
            approved=False,
            idempotency_key="short",
            adjustments=[BidModifierAgeAdjustment(adjustment_percent=-100)],
        )


def test_generic_bid_modifier_preview_supports_weather_metadata_and_direct_bid_modifier():
    request = BidModifiersUpdateRequest(
        approved=False,
        idempotency_key="weather-preview-123",
        adjustments=[
            BidModifierAdjustment(
                modifier_id=112233,
                type_hint="Weather",
                bid_modifier=80,
                conditions={"WeatherType": "RAIN", "Temperature": {"Operator": "LESS_THAN", "Value": 0}},
            )
        ],
    )

    assert request.build_payload_preview(710691939) == {
        "BidModifiers": [
            {
                "CampaignId": 710691939,
                "Id": 112233,
                "TypeHint": "Weather",
                "AdjustmentPercent": -20,
                "BidModifier": 80,
                "Conditions": {
                    "WeatherType": "RAIN",
                    "Temperature": {"Operator": "LESS_THAN", "Value": 0},
                },
            }
        ]
    }
    assert request.build_direct_set_payload() == {
        "BidModifiers": [{"Id": 112233, "BidModifier": 80}]
    }


def test_generic_bid_modifier_rejects_ambiguous_percent_and_direct_value():
    with pytest.raises(ValidationError):
        BidModifierAdjustment(adjustment_percent=-20, bid_modifier=80)


def test_generic_bid_modifier_requires_percent_or_direct_value():
    with pytest.raises(ValidationError):
        BidModifierAdjustment(modifier_id=112233)
