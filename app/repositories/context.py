from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

from app.repositories.protocols import LegacyStoreRepository

_REQUEST_REPOSITORY: ContextVar[LegacyStoreRepository | None] = ContextVar(
    "request_repository", default=None
)


def bind_request_repository(repository: LegacyStoreRepository) -> Token[LegacyStoreRepository | None]:
    return _REQUEST_REPOSITORY.set(repository)


def reset_request_repository(token: Token[LegacyStoreRepository | None]) -> None:
    _REQUEST_REPOSITORY.reset(token)


class RequestRepositoryProxy:
    """Resolve legacy store access to the repository bound to this request."""

    def __init__(self, fallback: LegacyStoreRepository) -> None:
        object.__setattr__(self, "_fallback", fallback)

    def _repository(self) -> LegacyStoreRepository:
        return _REQUEST_REPOSITORY.get() or self._fallback

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repository(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._repository(), name, value)
