from __future__ import annotations

from pydantic import ValidationError
import pytest

from app.models import (
    BidModifierCreateItem,
    BidModifiersCreateRequest,
)


@pytest.mark.parametrize(
    "scope,payload,expected_block",
    [
        (
            {"campaign_id": 710691939},
            {
                "type": "MOBILE_ADJUSTMENT",
                "adjustment_percent": -20,
                "operating_system_type": "IOS",
            },
            {"MobileAdjustment": {"BidModifier": 80, "OperatingSystemType": "IOS"}},
        ),
        (
            {"campaign_id": 710691939},
            {
                "type": "TABLET_ADJUSTMENT",
                "adjustment_percent": 5,
                "operating_system_type": "ANDROID",
            },
            {
                "TabletAdjustment": {
                    "BidModifier": 105,
                    "OperatingSystemType": "ANDROID",
                }
            },
        ),
        (
            {"campaign_id": 710691939},
            {"type": "DESKTOP_ADJUSTMENT", "adjustment_percent": 0},
            {"DesktopAdjustment": {"BidModifier": 100}},
        ),
        (
            {"campaign_id": 710691939},
            {"type": "DESKTOP_ONLY_ADJUSTMENT", "adjustment_percent": 10},
            {"DesktopOnlyAdjustment": {"BidModifier": 110}},
        ),
        (
            {"campaign_id": 710691939},
            {"type": "SMART_TV_ADJUSTMENT", "adjustment_percent": 10},
            {"SmartTvAdjustment": {"BidModifier": 110}},
        ),
        (
            {"campaign_id": 710691939},
            {"type": "SMARTTV_ADJUSTMENT", "adjustment_percent": 10},
            {"SmartTvAdjustment": {"BidModifier": 110}},
        ),
        (
            {"campaign_id": 710691939},
            {
                "type": "DEMOGRAPHICS_ADJUSTMENT",
                "adjustment_percent": 15,
                "gender": "GENDER_MALE",
                "age": "AGE_18_24",
            },
            {
                "DemographicsAdjustments": [
                    {
                        "BidModifier": 115,
                        "Gender": "GENDER_MALE",
                        "Age": "AGE_18_24",
                    }
                ]
            },
        ),
        (
            {"campaign_id": 710691939},
            {
                "type": "RETARGETING_ADJUSTMENT",
                "adjustment_percent": -10,
                "retargeting_condition_id": 321,
            },
            {
                "RetargetingAdjustments": [
                    {"BidModifier": 90, "RetargetingConditionId": 321}
                ]
            },
        ),
        (
            {"campaign_id": 710691939},
            {
                "type": "REGIONAL_ADJUSTMENT",
                "adjustment_percent": 20,
                "region_id": 213,
            },
            {"RegionalAdjustments": [{"BidModifier": 120, "RegionId": 213}]},
        ),
        (
            {"campaign_id": 710691939},
            {"type": "VIDEO_ADJUSTMENT", "adjustment_percent": -20},
            {"VideoAdjustment": {"BidModifier": 80}},
        ),
        (
            {"campaign_id": 710691939},
            {"type": "VIDEO_EXTENSION_ADJUSTMENT", "adjustment_percent": -20},
            {"VideoAdjustment": {"BidModifier": 80}},
        ),
        (
            {"campaign_id": 710691939},
            {"type": "SMART_AD_ADJUSTMENT", "adjustment_percent": -20},
            {"SmartAdAdjustment": {"BidModifier": 80}},
        ),
        (
            {"campaign_id": 710691939},
            {
                "type": "SERP_LAYOUT_ADJUSTMENT",
                "adjustment_percent": 30,
                "serp_layout": "ALONE",
            },
            {"SerpLayoutAdjustments": [{"BidModifier": 130, "SerpLayout": "ALONE"}]},
        ),
        (
            {"campaign_id": 710691939},
            {
                "type": "INCOME_GRADE_ADJUSTMENT",
                "adjustment_percent": -20,
                "grade": "HIGH",
            },
            {"IncomeGradeAdjustments": [{"BidModifier": 80, "Grade": "HIGH"}]},
        ),
        (
            {"ad_group_id": 987654321},
            {"type": "AD_GROUP_ADJUSTMENT", "adjustment_percent": 20},
            {"AdGroupAdjustment": {"BidModifier": 120}},
        ),
    ],
)
def test_bid_modifier_create_payload_contract_supported_families(
    scope: dict, payload: dict, expected_block: dict
):
    request = BidModifiersCreateRequest(items=[BidModifierCreateItem(**scope, **payload)])

    assert request.build_direct_add_payload() == {
        "BidModifiers": [
            {**({"CampaignId": scope["campaign_id"]} if "campaign_id" in scope else {"AdGroupId": scope["ad_group_id"]}), **expected_block},
        ]
    }


def test_bid_modifier_create_adjustment_percent_converts_to_bid_modifier():
    item = BidModifierCreateItem(
        campaign_id=1,
        type="MOBILE_ADJUSTMENT",
        adjustment_percent=-20,
        operating_system_type="IOS",
    )
    payload = item.to_direct_add_item()
    block = payload["MobileAdjustment"]
    assert block["BidModifier"] == 80


def test_bid_modifier_create_direct_bid_modifier_passthrough():
    item = BidModifierCreateItem(
        campaign_id=1,
        type="DESKTOP_ADJUSTMENT",
        bid_modifier=80,
    )
    payload = item.to_direct_add_item()
    assert payload["DesktopAdjustment"]["BidModifier"] == 80


def test_bid_modifier_create_missing_scope_is_rejected():
    with pytest.raises(ValidationError, match="campaign_id or ad_group_id"):
        BidModifierCreateItem(type="MOBILE_ADJUSTMENT", adjustment_percent=10)


def test_bid_modifier_create_multiple_scopes_are_rejected():
    with pytest.raises(ValidationError, match="Only one of campaign_id or ad_group_id is allowed"):
        BidModifierCreateItem(
            campaign_id=1,
            ad_group_id=2,
            type="MOBILE_ADJUSTMENT",
            adjustment_percent=10,
        )


def test_bid_modifier_create_weather_adjustment_is_rejected_after_live_error_8000():
    with pytest.raises(ValidationError, match="WEATHER_ADJUSTMENT create is not supported"):
        BidModifierCreateItem(
            campaign_id=1,
            type="WEATHER_ADJUSTMENT",
            adjustment_percent=-20,
            weather_type="RAIN",
            temperature={"Operator": "LESS_THAN", "Value": 0},
        )


def test_bid_modifier_create_weather_rejection_mentions_unknown_provider_parameter():
    with pytest.raises(ValidationError, match="WeatherAdjustment"):
        BidModifierCreateItem(
            campaign_id=1,
            type="WEATHER_ADJUSTMENT",
            adjustment_percent=10,
        )


def test_bid_modifier_create_regional_requires_region_id():
    with pytest.raises(ValidationError, match="REGIONAL_ADJUSTMENT requires"):
        BidModifierCreateItem(campaign_id=1, type="REGIONAL_ADJUSTMENT", adjustment_percent=10)


def test_bid_modifier_create_value_fields_are_mutually_exclusive():
    with pytest.raises(ValidationError, match="either adjustment_percent or bid_modifier"):
        BidModifierCreateItem(
            campaign_id=1,
            type="MOBILE_ADJUSTMENT",
            adjustment_percent=-20,
            bid_modifier=80,
        )


def test_bid_modifier_create_ad_group_scope_is_supported_for_regular_families():
    item = BidModifierCreateItem(
        ad_group_id=987,
        type="MOBILE_ADJUSTMENT",
        adjustment_percent=10,
        operating_system_type="IOS",
    )

    assert item.to_direct_add_item()["AdGroupId"] == 987
    assert "CampaignId" not in item.to_direct_add_item()


def test_bid_modifier_create_ad_group_adjustment_requires_ad_group_scope():
    with pytest.raises(ValidationError, match="AD_GROUP_ADJUSTMENT requires ad_group_id"):
        BidModifierCreateItem(
            campaign_id=1,
            type="AD_GROUP_ADJUSTMENT",
            adjustment_percent=10,
        )


def test_bid_modifier_create_value_required():
    with pytest.raises(ValidationError, match="adjustment_percent or bid_modifier is required"):
        BidModifierCreateItem(campaign_id=1, type="MOBILE_ADJUSTMENT")


@pytest.mark.parametrize(
    "payload,error_match",
    [
        ({"type": "MOBILE_ADJUSTMENT", "operating_system_type": "WINDOWS"}, "operating_system_type"),
        ({"type": "DEMOGRAPHICS_ADJUSTMENT", "gender": "MALE"}, "gender"),
        ({"type": "DEMOGRAPHICS_ADJUSTMENT", "age": "AGE_65_PLUS"}, "age"),
        ({"type": "SERP_LAYOUT_ADJUSTMENT", "serp_layout": "GRID"}, "serp_layout"),
        ({"type": "INCOME_GRADE_ADJUSTMENT", "grade": "LOW"}, "grade"),
    ],
)
def test_bid_modifier_create_rejects_non_official_enum_values(payload: dict, error_match: str):
    with pytest.raises(ValidationError, match=error_match):
        BidModifierCreateItem(campaign_id=1, adjustment_percent=10, **payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "DEMOGRAPHICS_ADJUSTMENT", "gender": "GENDER_FEMALE"},
        {"type": "RETARGETING_ADJUSTMENT", "retargeting_condition_id": 321},
        {"type": "REGIONAL_ADJUSTMENT", "region_id": 213},
        {"type": "SERP_LAYOUT_ADJUSTMENT", "serp_layout": "SUGGEST"},
        {"type": "INCOME_GRADE_ADJUSTMENT", "grade": "ABOVE_AVERAGE"},
    ],
)
def test_bid_modifier_create_rejects_enabled_on_add_payload_families(payload: dict):
    with pytest.raises(ValidationError, match="enabled"):
        BidModifierCreateItem(
            campaign_id=1,
            adjustment_percent=10,
            enabled=True,
            **payload,
        )
