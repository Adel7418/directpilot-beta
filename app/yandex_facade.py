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
            {"query": "сантехник на дом казань", "impressions": 540, "clicks": 22, "ctr": 4.07},
            {"query": "вызов электрика недорого", "impressions": 320, "clicks": 11, "ctr": 3.44},
            {"query": "ремонт квартир под ключ", "impressions": 410, "clicks": 9, "ctr": 2.20},
        ]


mock_yandex = MockYandexCatalog()


def _ensure_iterable(items: Iterable | None) -> list:
    return list(items) if items else []
