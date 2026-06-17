"""Deterministic in-memory mock of Yandex Direct read-only data.

This module intentionally performs no network I/O. It is used by the
Yandex Direct read-only facade to expose the shape of real Direct responses
without touching the live API.
"""

from __future__ import annotations

from itertools import count
from typing import Iterable


class MockYandexCatalog:
    """Deterministic, hand-crafted snapshots for tests and demo UI."""

    def __init__(self) -> None:
        self._search_query_counter = count(1)

    # --- campaigns ---------------------------------------------------------

    def list_campaigns(self) -> list[dict]:
        return [
            {
                "id": "cmp_mock_local_services",
                "name": "Mock: локальные услуги",
                "status": "active",
                "type": "text_campaign",
                "daily_budget": 1500.0,
            },
            {
                "id": "cmp_mock_remont_kazan",
                "name": "Mock: ремонт Казань",
                "status": "active",
                "type": "text_campaign",
                "daily_budget": 2500.0,
            },
        ]

    # --- ad groups ---------------------------------------------------------

    def list_ad_groups(self, campaign_id: str) -> list[dict]:
        groups_by_campaign: dict[str, list[dict]] = {
            "cmp_mock_local_services": [
                {
                    "id": "adg_mock_1001",
                    "campaign_id": "cmp_mock_local_services",
                    "name": "Услуги сантехника",
                    "status": "active",
                },
                {
                    "id": "adg_mock_1002",
                    "campaign_id": "cmp_mock_local_services",
                    "name": "Услуги электрика",
                    "status": "active",
                },
            ],
            "cmp_mock_remont_kazan": [
                {
                    "id": "adg_mock_2001",
                    "campaign_id": "cmp_mock_remont_kazan",
                    "name": "Ремонт квартир",
                    "status": "active",
                },
                {
                    "id": "adg_mock_2002",
                    "campaign_id": "cmp_mock_remont_kazan",
                    "name": "Ремонт офисов",
                    "status": "paused",
                },
            ],
        }
        return groups_by_campaign.get(campaign_id, [])

    # --- ads ---------------------------------------------------------------

    def list_ads(self, campaign_id: str) -> list[dict]:
        ads_by_campaign: dict[str, list[dict]] = {
            "cmp_mock_local_services": [
                {
                    "id": "ad_mock_3001",
                    "ad_group_id": "adg_mock_1001",
                    "campaign_id": "cmp_mock_local_services",
                    "title": "Сантехник на дом — выезд за 30 мин",
                    "status": "active",
                },
                {
                    "id": "ad_mock_3002",
                    "ad_group_id": "adg_mock_1002",
                    "campaign_id": "cmp_mock_local_services",
                    "title": "Электрик — срочный вызов",
                    "status": "active",
                },
            ],
            "cmp_mock_remont_kazan": [
                {
                    "id": "ad_mock_4001",
                    "ad_group_id": "adg_mock_2001",
                    "campaign_id": "cmp_mock_remont_kazan",
                    "title": "Ремонт квартир под ключ",
                    "status": "active",
                },
            ],
        }
        return ads_by_campaign.get(campaign_id, [])

    # --- ads detailed (for UTM audit) --------------------------------------

    def list_ads_detailed(self, campaign_id: str) -> list[dict]:
        """Mock ads with TextAd fields for UTM audit/plan."""
        detailed: dict[str, list[dict]] = {
            "cmp_mock_local_services": [
                {
                    "Id": 3001,
                    "AdGroupId": 1001,
                    "CampaignId": 100,
                    "Status": "ACCEPTED",
                    "State": "ON",
                    "Type": "TEXT_AD",
                    "TextAd": {
                        "Title": "Сантехник на дом — выезд за 30 мин",
                        "Text": "Услуги сантехника в Казани. Быстро, аккуратно, гарантия.",
                        "Href": "https://example.ru/santehnik?sort=price#services",
                        "SitelinkSetId": 5001,
                    },
                },
                {
                    "Id": 3002,
                    "AdGroupId": 1002,
                    "CampaignId": 100,
                    "Status": "ACCEPTED",
                    "State": "ON",
                    "Type": "TEXT_AD",
                    "TextAd": {
                        "Title": "Электрик — срочный вызов",
                        "Text": "Вызов электрика на дом. Срочно, круглосуточно.",
                        "Href": "https://example.ru/electric?utm_source=yandex&utm_medium=cpc",
                    },
                },
            ],
        }
        return detailed.get(campaign_id, [])

    # --- sitelinks mock ----------------------------------------------------

    def list_sitelinks(self, ids: list[int]) -> list[dict]:
        """Mock sitelink sets for UTM audit."""
        sets: dict[int, dict] = {
            5001: {
                "Id": 5001,
                "Sitelinks": [
                    {"Title": "Цены", "Href": "https://example.ru/prices#price-list"},
                    {"Title": "Отзывы", "Href": "https://example.ru/reviews?sort=new"},
                ],
            },
        }
        result = []
        for sid in ids:
            if sid in sets:
                result.append(sets[sid])
        return result

    # --- keywords ----------------------------------------------------------

    def list_keywords(self, campaign_id: str) -> list[dict]:
        kw_by_campaign: dict[str, list[dict]] = {
            "cmp_mock_local_services": [
                {"id": "kw_mock_5001", "ad_group_id": "adg_mock_1001", "phrase": "сантехник на дом", "status": "active"},
                {"id": "kw_mock_5002", "ad_group_id": "adg_mock_1001", "phrase": "вызов сантехника", "status": "active"},
                {"id": "kw_mock_5003", "ad_group_id": "adg_mock_1002", "phrase": "электрик на дом", "status": "active"},
            ],
            "cmp_mock_remont_kazan": [
                {"id": "kw_mock_6001", "ad_group_id": "adg_mock_2001", "phrase": "ремонт квартир казань", "status": "active"},
                {"id": "kw_mock_6002", "ad_group_id": "adg_mock_2001", "phrase": "ремонт под ключ казань", "status": "active"},
            ],
        }
        return kw_by_campaign.get(campaign_id, [])

    # --- reports -----------------------------------------------------------

    def report_summary(self) -> dict:
        return {
            "period": "last_7_days",
            "spend": 1250.0,
            "clicks": 42,
            "impressions": 2100,
            "ctr": 2.0,
            "cpc": 29.76,
            "conversions": None,
            "cpa": None,
        }

    def search_queries(self) -> list[dict]:
        return [
            {
                "query": "сантехник на дом казань",
                "campaign_id": "710691939",
                "campaign_name": "cmp_mock_local_services",
                "ad_group_id": "1001",
                "impressions": 540,
                "clicks": 22,
                "ctr": 4.07,
                "cost": 12.34,
            },
            {
                "query": "вызов электрика недорого",
                "campaign_id": "710691940",
                "campaign_name": "cmp_mock_el_service",
                "ad_group_id": "1002",
                "impressions": 320,
                "clicks": 11,
                "ctr": 3.44,
                "cost": 8.55,
            },
            {
                "query": "ремонт квартир под ключ",
                "campaign_id": "710691939",
                "campaign_name": "cmp_mock_remont_kazan",
                "ad_group_id": "2001",
                "impressions": 410,
                "clicks": 9,
                "ctr": 2.20,
                "cost": 5.11,
            },
        ]

    # --- time targeting read -------------------------------------------------

    def mock_time_targeting(self, campaign_id: str) -> dict:
        """Deterministic mock TimeTargeting block for a given campaign.

        Returns a dict with ``campaign_name`` and ``time_targeting``
        (v5 shape: ``Schedule.Items`` of 7 day-number + 24 bid-percents
        strings, ``ConsiderWorkingWeekends``, ``HolidaysSchedule``).
        """
        # Mon-Fri: 08:00-22:00 (100); Sat-Sun: 10:00-18:00 (100).
        weekday_hours = [0] * 8 + [100] * 14 + [0] * 2   # 08-22
        weekend_hours = [0] * 10 + [100] * 8 + [0] * 6   # 10-18
        all_hours = [weekday_hours] * 5 + [weekend_hours] * 2

        items = [
            f"{day_num},{','.join(str(v) for v in all_hours[day_num - 1])}"
            for day_num in range(1, 8)
        ]

        campaign_names: dict[str, str] = {
            "cmp_mock_local_services": "Mock: локальные услуги",
            "cmp_mock_remont_kazan": "Mock: ремонт Казань",
        }
        return {
            "campaign_name": campaign_names.get(
                campaign_id, f"Mock campaign {campaign_id}"
            ),
            "time_targeting": {
                "Schedule": {"Items": items},
                "ConsiderWorkingWeekends": "NO",
                "HolidaysSchedule": None,
            },
        }

    # --- strategy read -------------------------------------------------------

    def mock_strategy(self, campaign_id: str) -> dict:
        """Deterministic mock strategy block for a given campaign.

        Returns a dict with ``campaign_name``, ``campaign_type``,
        ``state``, ``status``, ``daily_budget``, ``counter_ids``,
        ``strategy`` (raw BiddingStrategy), and ``strategy_summary``.
        Mirrors the shape of ``YandexStrategyReadResult``.
        """
        campaign_names: dict[str, str] = {
            "cmp_mock_local_services": "Mock: локальные услуги",
            "cmp_mock_remont_kazan": "Mock: ремонт Казань",
        }
        return {
            "campaign_name": campaign_names.get(
                campaign_id, f"Mock campaign {campaign_id}"
            ),
            "campaign_type": "TEXT_CAMPAIGN",
            "state": "ON",
            "status": "ACCEPTED",
            "daily_budget": {
                "Amount": 5000000000,
                "SpendMode": "STANDARD",
            },
            "counter_ids": [123456],
            "strategy": {
                "Search": {
                    "BiddingStrategyType": "HIGHEST_POSITION",
                },
                "Network": {
                    "BiddingStrategyType": "SERVING_OFF",
                },
            },
            "strategy_summary": {
                "search": {
                    "type": "HIGHEST_POSITION",
                },
                "network": {
                    "type": "SERVING_OFF",
                },
            },
        }

    # --- autotargeting -------------------------------------------------------

    def list_autotargeting(self, campaign_id: str) -> list[dict]:
        """Deterministic mock autotargeting rows for a campaign.

        Each dict matches the shape of ``YandexAutotargetingReadItem``.
        Default preset: exact_narrow (Exact=YES, Narrow=YES,
        Alternative=NO, Accessory=NO, Broader=NO).
        Brand options: WithoutBrands=YES, WithAdvertiserBrand=YES,
        WithCompetitorsBrand=NO.
        """
        autotargeting_by_campaign: dict[str, list[dict]] = {
            "cmp_mock_local_services": [
                {
                    "ad_group_id": "adg_mock_1001",
                    "ad_group_name": "Услуги сантехника",
                    "autotargeting_keyword_id": "kw_auto_1001",
                    "status": "ACCEPTED",
                    "state": "ON",
                    "serving_status": "ELIGIBLE",
                    "categories": {
                        "Exact": "YES",
                        "Narrow": "YES",
                        "Alternative": "NO",
                        "Accessory": "NO",
                        "Broader": "NO",
                    },
                    "brand_options": {
                        "WithoutBrands": "YES",
                        "WithAdvertiserBrand": "YES",
                        "WithCompetitorsBrand": "NO",
                    },
                    "raw_provider": {"Id": "kw_auto_1001", "Keyword": "---autotargeting"},
                },
                {
                    "ad_group_id": "adg_mock_1002",
                    "ad_group_name": "Услуги электрика",
                    "autotargeting_keyword_id": "kw_auto_1002",
                    "status": "ACCEPTED",
                    "state": "ON",
                    "serving_status": "ELIGIBLE",
                    "categories": {
                        "Exact": "YES",
                        "Narrow": "YES",
                        "Alternative": "NO",
                        "Accessory": "NO",
                        "Broader": "NO",
                    },
                    "brand_options": {
                        "WithoutBrands": "YES",
                        "WithAdvertiserBrand": "YES",
                        "WithCompetitorsBrand": "NO",
                    },
                    "raw_provider": {"Id": "kw_auto_1002", "Keyword": "---autotargeting"},
                },
            ],
            "cmp_mock_remont_kazan": [
                {
                    "ad_group_id": "adg_mock_2001",
                    "ad_group_name": "Ремонт квартир",
                    "autotargeting_keyword_id": "kw_auto_2001",
                    "status": "ACCEPTED",
                    "state": "ON",
                    "serving_status": "ELIGIBLE",
                    "categories": {
                        "Exact": "YES",
                        "Narrow": "YES",
                        "Alternative": "NO",
                        "Accessory": "NO",
                        "Broader": "NO",
                    },
                    "brand_options": {
                        "WithoutBrands": "YES",
                        "WithAdvertiserBrand": "YES",
                        "WithCompetitorsBrand": "NO",
                    },
                    "raw_provider": {"Id": "kw_auto_2001", "Keyword": "---autotargeting"},
                },
                {
                    "ad_group_id": "adg_mock_2002",
                    "ad_group_name": "Ремонт офисов",
                    "autotargeting_keyword_id": "kw_auto_2002",
                    "status": "ACCEPTED",
                    "state": "ON",
                    "serving_status": "ELIGIBLE",
                    "categories": {
                        "Exact": "YES",
                        "Narrow": "YES",
                        "Alternative": "NO",
                        "Accessory": "NO",
                        "Broader": "NO",
                    },
                    "brand_options": {
                        "WithoutBrands": "YES",
                        "WithAdvertiserBrand": "YES",
                        "WithCompetitorsBrand": "NO",
                    },
                    "raw_provider": {"Id": "kw_auto_2002", "Keyword": "---autotargeting"},
                },
            ],
        }
        return autotargeting_by_campaign.get(campaign_id, [])


mock_yandex = MockYandexCatalog()


def _ensure_iterable(items: Iterable | None) -> list:
    return list(items) if items else []
