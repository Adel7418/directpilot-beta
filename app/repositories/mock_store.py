from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.store import MockStore


class MockStoreRepositoryAdapter:
    """Adapter preserving the existing MockStore behavior behind a seam."""

    def __init__(self, store: MockStore) -> None:
        self._store = store

    @property
    def campaigns(self) -> Mapping[Any, Any]:
        return self._store.campaigns

    @property
    def drafts(self) -> Mapping[Any, Any]:
        return self._store.drafts

    @property
    def recommendations(self) -> Mapping[Any, Any]:
        return self._store.recommendations

    @property
    def audit_events(self) -> list[Any]:
        return self._store.audit_events

    def create_draft(self, payload: Any) -> Any:
        return self._store.create_draft(payload)

    def append_audit(self, *args: Any, **kwargs: Any) -> Any:
        return self._store.append_audit(*args, **kwargs)

    def yandex_control(self, *args: Any, **kwargs: Any) -> Any:
        return self._store.yandex_control(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)
