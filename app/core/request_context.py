from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import uuid4

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.bootstrap.dependencies import get_repository
from app.core.errors import is_api_v1_path, safe_error_response
from app.core.logging import log_request_completed, log_request_failed
from app.repositories.context import bind_request_repository, reset_request_repository

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "request_context", default=None
)


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str


def create_request_context(request_id: str | None) -> RequestContext:
    """Create a request context without echoing untrusted identifier values."""
    if request_id and _REQUEST_ID_PATTERN.fullmatch(request_id):
        return RequestContext(request_id=request_id)
    return RequestContext(request_id=uuid4().hex)


def get_request_context() -> RequestContext:
    context = _REQUEST_CONTEXT.get()
    if context is None:
        raise RuntimeError("RequestContext is only available while handling a request")
    return context


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        context = create_request_context(request.headers.get("X-Request-ID"))
        token: Token[RequestContext | None] = _REQUEST_CONTEXT.set(context)
        repository_token = bind_request_repository(get_repository(request))
        request.state.request_context = context
        try:
            try:
                response = await call_next(request)
            except Exception:
                log_request_failed(
                    request_id=context.request_id,
                    method=request.method,
                    path=request.url.path,
                )
                if not is_api_v1_path(request.url.path):
                    raise
                response = safe_error_response(
                    request=request,
                    status_code=500,
                    code="internal_error",
                )
            response.headers["X-Request-ID"] = context.request_id
            log_request_completed(
                request_id=context.request_id,
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
            )
            return response
        finally:
            reset_request_repository(repository_token)
            _REQUEST_CONTEXT.reset(token)
