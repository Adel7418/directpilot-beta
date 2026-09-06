from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.responses import RedirectResponse, Response

from app.bootstrap.dependencies import get_application_dependencies
from app.core.errors import safe_error_response
from app.modules.integrations.yandex.credentials import CredentialVaultError
from app.modules.integrations.yandex.oauth import (
    OAuthCallbackRejected,
    OAuthCallbackService,
    OAuthClientMismatch,
    OAuthCredentialPersistenceUnavailable,
    OAuthStartService,
    YandexOAuthConfiguration,
    YandexOAuthIntegration,
    YandexOAuthProvider,
)
from app.modules.integrations.yandex.provider import (
    HttpxYandexOAuthProvider,
    YandexOAuthProviderFailure,
)
from app.modules.integrations.yandex.refresh import (
    YandexConnectionCredentialUnavailable,
    YandexConnectionLifecycle,
    YandexConnectionNotActive,
    YandexConnectionNotFound,
    YandexConnectionPersistenceFailure,
    YandexConnectionProviderConfigurationFailure,
    YandexConnectionProviderUnavailable,
    YandexConnectionReauthorizationRequired,
)
from app.modules.integrations.yandex.repository import (
    ExternalIdentityConflict,
    ProviderConnectionPersistenceError,
)
from app.modules.sessions.cookies import SESSION_COOKIE_NAME, set_session_cookie
from app.modules.sessions.service import AuthenticatedSession, PostgresSessionService
from app.modules.tenancy.policy import AuthorizationDenied, Capability

router = APIRouter()


class YandexConnectionRefreshResponse(BaseModel):
    connection_id: UUID
    status: Literal["active"]
    refreshed: bool
    access_token_expires_at: datetime
    credential_version: int


class YandexConnectionDisconnectResponse(BaseModel):
    connection_id: UUID
    status: Literal["disconnected"]
    local_credentials_purged: Literal[True]
    provider_revocation: Literal["not_supported_for_current_grant"]
    yandex_revocation_url: Literal["https://id.yandex.ru/personal/data-access"]


@dataclass(frozen=True, slots=True)
class _OAuthBrowserSession:
    token: str = field(repr=False)
    principal: AuthenticatedSession
    service: PostgresSessionService


def _safe_error(request: Request, *, status_code: int, code: str) -> Response:
    return safe_error_response(request=request, status_code=status_code, code=code)


def _authenticated_oauth_session(request: Request) -> _OAuthBrowserSession | Response:
    dependencies = get_application_dependencies(request)
    session_service = dependencies.session_service
    authorizer = dependencies.workspace_authorizer
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token is None or session_service is None or authorizer is None:
        return _safe_error(request, status_code=401, code="oauth_authentication_required")
    principal = session_service.authenticate(token, touch=False)
    if principal is None or principal.active_workspace_id is None:
        return _safe_error(request, status_code=401, code="oauth_authentication_required")
    try:
        authorizer.authorize(
            user_id=principal.user_id,
            workspace_id=principal.active_workspace_id,
            capability=Capability.MANAGE_PROVIDER_CONNECTIONS,
        )
    except AuthorizationDenied:
        return _safe_error(request, status_code=403, code="oauth_workspace_access_denied")
    return _OAuthBrowserSession(token=token, principal=principal, service=session_service)


def _configured_integration(
    request: Request,
) -> tuple[YandexOAuthIntegration, YandexOAuthConfiguration] | Response:
    integration = get_application_dependencies(request).yandex_oauth
    if (
        integration is None
        or integration.config is None
        or integration.credential_persister is None
    ):
        return _safe_error(request, status_code=503, code="oauth_unavailable")
    return integration, integration.config


def _configured_lifecycle(request: Request) -> YandexConnectionLifecycle | Response:
    lifecycle = get_application_dependencies(request).yandex_connection_lifecycle
    if lifecycle is None:
        return _safe_error(request, status_code=503, code="oauth_unavailable")
    return lifecycle


def _empty_body_or_error(request: Request) -> Response | None:
    content_length = request.headers.get("content-length")
    if content_length not in {None, "0"} or request.headers.get("transfer-encoding") is not None:
        return _safe_error(request, status_code=400, code="oauth_request_body_not_allowed")
    return None


def _lifecycle_error_response(
    request: Request,
    *,
    error: Exception,
    disconnect: bool,
) -> Response:
    if isinstance(error, YandexConnectionNotFound):
        return _safe_error(request, status_code=404, code="connection_not_found")
    if isinstance(error, YandexConnectionNotActive):
        return _safe_error(request, status_code=409, code="connection_not_active")
    if isinstance(error, YandexConnectionReauthorizationRequired):
        return _safe_error(request, status_code=409, code="reauthorization_required")
    if isinstance(error, YandexConnectionCredentialUnavailable):
        return _safe_error(request, status_code=503, code="credential_unavailable")
    if isinstance(error, YandexConnectionPersistenceFailure):
        return _safe_error(
            request,
            status_code=503,
            code="disconnect_persistence_failed" if disconnect else "refresh_persistence_failed",
        )
    if isinstance(error, YandexConnectionProviderConfigurationFailure):
        return _safe_error(request, status_code=503, code="oauth_unavailable")
    if isinstance(error, YandexConnectionProviderUnavailable):
        return _safe_error(request, status_code=502, code="oauth_provider_unavailable")
    return _safe_error(request, status_code=503, code="oauth_unavailable")


def _provider(
    integration: YandexOAuthIntegration,
    config: YandexOAuthConfiguration,
) -> YandexOAuthProvider:
    return integration.provider or HttpxYandexOAuthProvider(config=config)


@router.post(
    "/api/v1/integrations/yandex/connections/{connection_id}/refresh",
    response_model=YandexConnectionRefreshResponse,
)
def refresh_yandex_connection(
    request: Request,
    connection_id: UUID,
) -> YandexConnectionRefreshResponse | Response:
    browser = _authenticated_oauth_session(request)
    if isinstance(browser, Response):
        return browser
    body_error = _empty_body_or_error(request)
    if body_error is not None:
        return body_error
    lifecycle = _configured_lifecycle(request)
    if isinstance(lifecycle, Response):
        return lifecycle
    workspace_id = browser.principal.active_workspace_id
    if workspace_id is None:
        return _safe_error(request, status_code=401, code="oauth_authentication_required")
    try:
        result = lifecycle.refresh(
            user_id=browser.principal.user_id,
            workspace_id=workspace_id,
            connection_id=connection_id,
        )
    except (
        YandexConnectionNotFound,
        YandexConnectionNotActive,
        YandexConnectionReauthorizationRequired,
        YandexConnectionCredentialUnavailable,
        YandexConnectionPersistenceFailure,
        YandexConnectionProviderConfigurationFailure,
        YandexConnectionProviderUnavailable,
    ) as error:
        return _lifecycle_error_response(request, error=error, disconnect=False)
    return YandexConnectionRefreshResponse(
        connection_id=result.connection_id,
        status="active",
        refreshed=result.refreshed,
        access_token_expires_at=result.access_token_expires_at,
        credential_version=result.credential_version,
    )


@router.post(
    "/api/v1/integrations/yandex/connections/{connection_id}/disconnect",
    response_model=YandexConnectionDisconnectResponse,
)
def disconnect_yandex_connection(
    request: Request,
    connection_id: UUID,
) -> YandexConnectionDisconnectResponse | Response:
    browser = _authenticated_oauth_session(request)
    if isinstance(browser, Response):
        return browser
    body_error = _empty_body_or_error(request)
    if body_error is not None:
        return body_error
    lifecycle = _configured_lifecycle(request)
    if isinstance(lifecycle, Response):
        return lifecycle
    workspace_id = browser.principal.active_workspace_id
    if workspace_id is None:
        return _safe_error(request, status_code=401, code="oauth_authentication_required")
    try:
        result = lifecycle.disconnect(
            user_id=browser.principal.user_id,
            workspace_id=workspace_id,
            connection_id=connection_id,
        )
    except (
        YandexConnectionNotFound,
        YandexConnectionNotActive,
        YandexConnectionReauthorizationRequired,
        YandexConnectionCredentialUnavailable,
        YandexConnectionPersistenceFailure,
        YandexConnectionProviderConfigurationFailure,
        YandexConnectionProviderUnavailable,
    ) as error:
        return _lifecycle_error_response(request, error=error, disconnect=True)
    return YandexConnectionDisconnectResponse(
        connection_id=result.connection_id,
        status="disconnected",
        local_credentials_purged=True,
        provider_revocation="not_supported_for_current_grant",
        yandex_revocation_url="https://id.yandex.ru/personal/data-access",
    )


@router.get("/api/v1/integrations/yandex/start")
def start_yandex_oauth(request: Request, return_path: str = "/") -> Response:
    browser = _authenticated_oauth_session(request)
    if isinstance(browser, Response):
        return browser
    configured = _configured_integration(request)
    if isinstance(configured, Response):
        return configured
    integration, config = configured
    workspace_id = browser.principal.active_workspace_id
    if workspace_id is None:
        return _safe_error(request, status_code=401, code="oauth_authentication_required")
    try:
        started = OAuthStartService(config=config, transactions=integration.transactions).start(
            user_id=browser.principal.user_id,
            workspace_id=workspace_id,
            browser_session_id=browser.principal.id,
            return_path=return_path,
        )
    except ValueError:
        return _safe_error(request, status_code=400, code="oauth_start_rejected")
    return RedirectResponse(url=started.authorization_url, status_code=302)


@router.get("/api/v1/integrations/yandex/callback")
def complete_yandex_oauth(
    request: Request,
    code: str | None = None,
    state: str | None = None,
) -> Response:
    if code is None or state is None:
        return _safe_error(request, status_code=400, code="oauth_callback_rejected")
    browser = _authenticated_oauth_session(request)
    if isinstance(browser, Response):
        return browser
    configured = _configured_integration(request)
    if isinstance(configured, Response):
        return configured
    integration, config = configured
    workspace_id = browser.principal.active_workspace_id
    if workspace_id is None:
        return _safe_error(request, status_code=401, code="oauth_authentication_required")
    try:
        completed = OAuthCallbackService(
            config=config,
            transactions=integration.transactions,
            identities=integration.identities,
            provider=_provider(integration, config),
            credential_persister=integration.credential_persister,
        ).complete(
            code=code,
            state=state,
            user_id=browser.principal.user_id,
            workspace_id=workspace_id,
            browser_session_id=browser.principal.id,
        )
    except OAuthCallbackRejected:
        return _safe_error(request, status_code=400, code="oauth_callback_rejected")
    except OAuthClientMismatch:
        return _safe_error(request, status_code=502, code="oauth_provider_error")
    except ExternalIdentityConflict:
        return _safe_error(request, status_code=409, code="oauth_identity_conflict")
    except OAuthCredentialPersistenceUnavailable:
        return _safe_error(request, status_code=503, code="oauth_credential_vault_unavailable")
    except (CredentialVaultError, ProviderConnectionPersistenceError):
        return _safe_error(request, status_code=503, code="oauth_credential_persistence_failed")
    except YandexOAuthProviderFailure as exc:
        if exc.kind == "invalid_grant":
            return _safe_error(request, status_code=400, code="oauth_reauthorization_required")
        if exc.kind in {"invalid_client", "unauthorized_client"}:
            return _safe_error(request, status_code=503, code="oauth_provider_configuration_error")
        return _safe_error(request, status_code=502, code="oauth_provider_error")

    rotated = browser.service.rotate(browser.token)
    if rotated is None:
        return _safe_error(request, status_code=401, code="oauth_session_rotation_failed")
    response = RedirectResponse(url=completed.return_path, status_code=303)
    set_session_cookie(response, token=rotated.token, expires_at=rotated.expires_at)
    return response
