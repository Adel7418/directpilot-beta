from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.core.request_context import (
    bind_authenticated_request_context,
    bind_authorized_request_repository,
)
from app.modules.sessions.cookies import SESSION_COOKIE_NAME
from app.modules.tenancy.policy import AuthorizationDenied, Capability
from app.repositories.context import reset_request_repository
from app.repositories.postgres_store import PostgresLegacyStoreRepository

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _same_origin(request: Request, origin: str) -> bool:
    parsed = urlsplit(origin)
    if parsed.path or parsed.query or parsed.fragment:
        return False
    return parsed.scheme == request.url.scheme and parsed.netloc == request.headers.get("host")


def _csrf_request_is_same_site(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is not None and not _same_origin(request, origin):
        return False
    return request.headers.get("sec-fetch-site", "").lower() != "cross-site"


class SessionWorkspaceContextMiddleware(BaseHTTPMiddleware):
    """Bind a repository only from the opaque session's authorized workspace."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        session_token = request.cookies.get(SESSION_COOKIE_NAME)
        if session_token is None:
            return await call_next(request)

        dependencies = request.app.state.dependencies
        service = getattr(dependencies, "session_service", None)
        authorizer = getattr(dependencies, "workspace_authorizer", None)
        repository = getattr(dependencies, "repository", None)
        if service is None or authorizer is None:
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "authentication_required"}},
            )

        principal = service.authenticate(session_token, touch=False)
        if principal is None or principal.active_workspace_id is None:
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "authentication_required"}},
            )
        try:
            membership = authorizer.authorize(
                user_id=principal.user_id,
                workspace_id=principal.active_workspace_id,
                capability=Capability.READ_WORKSPACE_DATA,
            )
        except AuthorizationDenied:
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "workspace_access_denied"}},
            )

        bind_authenticated_request_context(
            user_id=principal.user_id,
            session_id=principal.id,
            workspace_id=membership.workspace_id,
            membership_role=membership.role,
        )
        repository_token = None
        if isinstance(repository, PostgresLegacyStoreRepository):
            repository_token = bind_authorized_request_repository(
                repository.for_workspace(membership.workspace_id, membership.user_id)
            )
        try:
            return await call_next(request)
        finally:
            if repository_token is not None:
                reset_request_repository(repository_token)


class CookieCsrfMiddleware(BaseHTTPMiddleware):
    """Require a server-validated synchronizer value for unsafe cookie requests."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.method not in _SAFE_METHODS:
            session_token = request.cookies.get(SESSION_COOKIE_NAME)
            if session_token is not None:
                service = getattr(request.app.state.dependencies, "session_service", None)
                csrf_token = request.headers.get("X-CSRF-Token")
                if (
                    service is None
                    or csrf_token is None
                    or not _csrf_request_is_same_site(request)
                    or not service.verify_csrf(session_token, csrf_token)
                ):
                    return JSONResponse(
                        status_code=403,
                        content={"error": {"code": "csrf_validation_failed"}},
                    )
        return await call_next(request)
