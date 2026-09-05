from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, Response

from app.bootstrap.dependencies import get_application_dependencies
from app.core.request_context import bind_authenticated_request_context
from app.modules.identity.repository import PostgresIdentityRepository
from app.modules.sessions.cookies import (
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    set_session_cookie,
)
from app.modules.sessions.service import AuthenticatedSession, PostgresSessionService
from app.modules.tenancy.authorization import (
    AuthorizedMembership,
    PostgresWorkspaceAuthorizer,
)
from app.modules.tenancy.policy import AuthorizationDenied, Capability

router = APIRouter()


class _FakeLoginPayload(BaseModel):
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    workspace_name: Annotated[str | None, Field(min_length=1, max_length=128)] = None


def _fake_auth_services(
    request: Request,
) -> tuple[PostgresIdentityRepository, PostgresSessionService, PostgresWorkspaceAuthorizer]:
    dependencies = get_application_dependencies(request)
    if (
        not dependencies.fake_auth_enabled
        or dependencies.identity_repository is None
        or dependencies.session_service is None
        or dependencies.workspace_authorizer is None
    ):
        raise HTTPException(status_code=404, detail="not found")
    return (
        dependencies.identity_repository,
        dependencies.session_service,
        dependencies.workspace_authorizer,
    )


def _authorized_session(
    request: Request,
    *,
    capability: Capability,
) -> tuple[AuthenticatedSession, AuthorizedMembership]:
    _, service, authorizer = _fake_auth_services(request)
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token is None:
        raise HTTPException(status_code=401, detail="authentication required")
    principal = service.authenticate(token, touch=False)
    if principal is None or principal.active_workspace_id is None:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        membership = authorizer.authorize(
            user_id=principal.user_id,
            workspace_id=principal.active_workspace_id,
            capability=capability,
        )
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    bind_authenticated_request_context(
        user_id=principal.user_id,
        session_id=principal.id,
        workspace_id=membership.workspace_id,
        membership_role=membership.role,
    )
    return principal, membership


@router.post("/api/v1/_test/identity/login", include_in_schema=False)
def fake_login(payload: _FakeLoginPayload, request: Request) -> JSONResponse:
    identity_repository, session_service, _ = _fake_auth_services(request)
    workspace_name = payload.workspace_name or f"{payload.display_name.strip()} workspace"
    created = identity_repository.create_personal_workspace(
        display_name=payload.display_name,
        workspace_name=workspace_name,
    )
    issued = session_service.issue(
        user_id=created.user.id,
        active_workspace_id=created.workspace.id,
    )
    response = JSONResponse(
        {
            "user_id": str(created.user.id),
            "workspace_id": str(created.workspace.id),
            "csrf_token": issued.csrf_token,
        }
    )
    set_session_cookie(response, token=issued.token, expires_at=issued.expires_at)
    return response


@router.get("/api/v1/_test/identity/session", include_in_schema=False)
def current_fake_session(request: Request) -> dict[str, str]:
    principal, membership = _authorized_session(
        request,
        capability=Capability.READ_WORKSPACE_DATA,
    )
    return {
        "user_id": str(principal.user_id),
        "workspace_id": str(membership.workspace_id),
    }


@router.post("/api/v1/_test/identity/logout", include_in_schema=False)
def fake_logout(request: Request) -> Response:
    _, session_service, _ = _fake_auth_services(request)
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token is None or not session_service.logout(token):
        raise HTTPException(status_code=401, detail="authentication required")
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response
