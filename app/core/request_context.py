from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import UUID, uuid4

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.bootstrap.dependencies import get_repository
from app.core.errors import is_api_v1_path, safe_error_response
from app.core.logging import log_request_completed, log_request_failed
from app.modules.tenancy.models import MembershipRole
from app.repositories.context import bind_request_repository, reset_request_repository
from app.repositories.protocols import LegacyStoreRepository

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "request_context", default=None
)


@dataclass(slots=True)
class RequestContext:
    request_id: str
    user_id: UUID | None = None
    session_id: UUID | None = None
    workspace_id: UUID | None = None
    membership_role: MembershipRole | None = None


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


def bind_authenticated_request_context(
    *,
    user_id: UUID,
    session_id: UUID,
    workspace_id: UUID,
    membership_role: MembershipRole,
) -> RequestContext:
    """Attach freshly re-authorized tenant authority to this request only."""

    context = get_request_context()
    context.user_id = user_id
    context.session_id = session_id
    context.workspace_id = workspace_id
    context.membership_role = membership_role
    return context


def bind_authorized_request_repository(
    repository: LegacyStoreRepository,
) -> Token[LegacyStoreRepository | None]:
    """Bind a repository only after server-side workspace authorization."""

    get_request_context()
    return bind_request_repository(repository)


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
