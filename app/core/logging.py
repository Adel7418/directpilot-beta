from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

_REQUEST_LOGGER = logging.getLogger("directpilot.request")
_SENSITIVE_FIELD_MARKERS = frozenset(
    {
        "authorization",
        "credential",
        "cookie",
        "key",
        "password",
        "secret",
        "token",
    }
)
_BEARER_VALUE = re.compile(r"^Bearer\s+.+$", re.IGNORECASE)


def _is_sensitive_field(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_FIELD_MARKERS)


def redact_value(value: Any) -> Any:
    """Return a recursively redacted value suitable for structured logging."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _is_sensitive_field(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str) and _BEARER_VALUE.fullmatch(value):
        return "[REDACTED]"
    return value


def _log(event: str, **fields: Any) -> None:
    _REQUEST_LOGGER.info(event, extra={"event": event, **redact_value(fields)})


def log_request_completed(
    *, request_id: str, method: str, path: str, status_code: int
) -> None:
    _log(
        "request_completed",
        request_id=request_id,
        method=method,
        path=path,
        status_code=status_code,
    )


def log_request_failed(*, request_id: str, method: str, path: str) -> None:
    _log("request_failed", request_id=request_id, method=method, path=path)


def log_api_error(*, request_id: str, status_code: int) -> None:
    _log("api_error", request_id=request_id, status_code=status_code)
