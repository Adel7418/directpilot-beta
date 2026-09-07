"""Read-only client for Yandex Search API v2 Web Search.

This module is intentionally separate from ``YandexSearchWordstatClient``:
Web Search uses the protobuf-backed ``/v2/web/search`` endpoint, while
Wordstat uses a different v2 service family.

Provider protobuf definitions:
- https://raw.githubusercontent.com/yandex-cloud/cloudapi/master/yandex/cloud/searchapi/v2/search_service.proto
- https://raw.githubusercontent.com/yandex-cloud/cloudapi/master/yandex/cloud/searchapi/v2/search_query.proto

The provider returns XML encoded as base64 in ``rawData``. Raw provider data,
XML, credentials, and provider response bodies never leave this client.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from app.config import Settings


BASE_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"


class YandexWebSearchError(RuntimeError):
    """Safe base error for Yandex Search API v2 Web Search failures."""


class YandexWebSearchMissingKeyError(YandexWebSearchError):
    """Raised before network access when the Search API key is unavailable."""


class YandexWebSearchMissingFolderError(YandexWebSearchError):
    """Raised before network access when the required cloud folder is unavailable."""


class YandexWebSearchValidationError(YandexWebSearchError):
    """Raised for invalid public client inputs without echoing their values."""


class YandexWebSearchClient:
    """Thin synchronous client for the read-only Web Search v2 endpoint."""

    BASE_URL = BASE_URL

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.Client(timeout=20, transport=transport)

    def search_web(
        self,
        query_text: str,
        *,
        page: int = 0,
        region: str = "225",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search the Web API and return only normalized, safe result fields."""
        self._validate_arguments(query_text=query_text, page=page, region=region, limit=limit)

        api_key = self.settings.yandex_search_api_key
        if not api_key:
            raise YandexWebSearchMissingKeyError(
                "Yandex Search API Web Search is not configured"
            )

        folder_id = self.settings.yandex_search_folder_id
        if not folder_id:
            raise YandexWebSearchMissingFolderError(
                "Yandex Search API Web Search is not configured"
            )

        body = self._build_request(
            query_text=query_text,
            page=page,
            region=region,
            limit=limit,
            folder_id=folder_id,
        )
        headers = {"Authorization": f"Api-Key {api_key}"}

        try:
            response = self._client.post(self.BASE_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise YandexWebSearchError(
                f"Yandex Search API Web Search transport error: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            raise YandexWebSearchError(
                f"Yandex Search API Web Search HTTP {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise YandexWebSearchError(
                "Yandex Search API Web Search returned a non-JSON response"
            ) from exc

        return self._normalize_response(payload, query_text=query_text)

    @staticmethod
    def _validate_arguments(
        *,
        query_text: str,
        page: int,
        region: str,
        limit: int,
    ) -> None:
        if not isinstance(query_text, str) or not 1 <= len(query_text) <= 400:
            raise YandexWebSearchValidationError("query_text must be 1..400 characters")
        if not isinstance(page, int) or isinstance(page, bool) or page < 0:
            raise YandexWebSearchValidationError("page must be a non-negative integer")
        if not isinstance(region, str) or not region or len(region) > 100:
            raise YandexWebSearchValidationError("region must be a non-empty string up to 100 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise YandexWebSearchValidationError("limit must be an integer from 1 to 100")

    @staticmethod
    def _build_request(
        *,
        query_text: str,
        page: int,
        region: str,
        limit: int,
        folder_id: str,
    ) -> dict[str, Any]:
        """Build the documented protobuf JSON mapping with string int64 values."""
        return {
            "query": {
                "searchType": "SEARCH_TYPE_RU",
                "queryText": query_text,
                "familyMode": "FAMILY_MODE_MODERATE",
                "page": str(page),
                "fixTypoMode": "FIX_TYPO_MODE_ON",
            },
            "sortSpec": {
                "sortMode": "SORT_MODE_BY_RELEVANCE",
                "sortOrder": "SORT_ORDER_DESC",
            },
            "groupSpec": {
                "groupMode": "GROUP_MODE_FLAT",
                "groupsOnPage": str(limit),
                "docsInGroup": "1",
            },
            "maxPassages": "2",
            "region": region,
            "l10n": "LOCALIZATION_RU",
            "folderId": folder_id,
            "responseFormat": "FORMAT_XML",
        }

    @classmethod
    def _normalize_response(cls, payload: Any, *, query_text: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise YandexWebSearchError("Yandex Search API Web Search returned an invalid response")

        raw_data = payload.get("rawData")
        if not isinstance(raw_data, str) or not raw_data:
            raise YandexWebSearchError("Yandex Search API Web Search returned an invalid response")

        try:
            xml_bytes = base64.b64decode(raw_data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise YandexWebSearchError(
                "Yandex Search API Web Search returned invalid encoded data"
            ) from exc

        try:
            xml_text = xml_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise YandexWebSearchError(
                "Yandex Search API Web Search returned invalid UTF-8 data"
            ) from exc

        # ElementTree is intentionally used from the standard library. Reject
        # DTD/entity declarations before parsing so no entity expansion is
        # accepted from an upstream response.
        uppercase_xml = xml_text.upper()
        if "<!DOCTYPE" in uppercase_xml or "<!ENTITY" in uppercase_xml:
            raise YandexWebSearchError("Yandex Search API Web Search returned invalid XML")

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise YandexWebSearchError("Yandex Search API Web Search returned invalid XML") from exc

        if cls._local_name(root.tag) != "yandexsearch":
            raise YandexWebSearchError("Yandex Search API Web Search returned invalid XML")

        if any(cls._local_name(element.tag) == "error" for element in root.iter()):
            raise YandexWebSearchError("Yandex Search API Web Search returned a provider error")

        found = cls._parse_found(root)
        results = [cls._normalize_doc(document) for document in root.iter() if cls._local_name(document.tag) == "doc"]
        return {"query": query_text, "found": found, "results": results}

    @classmethod
    def _normalize_doc(cls, document: ET.Element) -> dict[str, Any]:
        title = cls._text_of(cls._first_descendant(document, "title")) or ""
        url = cls._text_of(cls._first_descendant(document, "url")) or ""
        domain = cls._text_of(cls._first_descendant(document, "domain"))
        headline = cls._text_of(cls._first_descendant(document, "headline"))
        passages = [
            passage_text
            for passage in document.iter()
            if cls._local_name(passage.tag) == "passage"
            if (passage_text := cls._text_of(passage))
        ]
        return {
            "title": title,
            "url": url,
            "domain": domain,
            "headline": headline,
            "passages": passages,
        }

    @classmethod
    def _parse_found(cls, root: ET.Element) -> int | None:
        found_text = cls._text_of(cls._first_descendant(root, "found"))
        if not found_text:
            return None
        try:
            return int(found_text)
        except ValueError:
            return None

    @classmethod
    def _first_descendant(cls, element: ET.Element, name: str) -> ET.Element | None:
        for candidate in element.iter():
            if candidate is not element and cls._local_name(candidate.tag) == name:
                return candidate
        return None

    @staticmethod
    def _text_of(element: ET.Element | None) -> str | None:
        if element is None:
            return None
        text = " ".join("".join(element.itertext()).split())
        return text or None

    @staticmethod
    def _local_name(tag: object) -> str:
        if not isinstance(tag, str):
            return ""
        return tag.rsplit("}", 1)[-1]
