from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LegacyStoreRepository(Protocol):
    """Small stable surface used by the legacy router during P1 extraction."""

    @property
    def campaigns(self) -> Mapping[Any, Any]: ...

    @property
    def drafts(self) -> Mapping[Any, Any]: ...

    @property
    def recommendations(self) -> Mapping[Any, Any]: ...

    @property
    def audit_events(self) -> list[Any]: ...

    def create_draft(self, payload: Any) -> Any: ...

    def append_audit(self, *args: Any, **kwargs: Any) -> Any: ...

    def yandex_control(self, *args: Any, **kwargs: Any) -> Any: ...
