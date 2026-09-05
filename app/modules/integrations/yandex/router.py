from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from starlette.responses import RedirectResponse, Response

from app.bootstrap.dependencies import get_application_dependencies
from app.core.errors import safe_error_response
from app.modules.integrations.yandex.oauth import (
    OAuthCallbackRejected,
    OAuthCallbackService,
    OAuthClientMismatch,
    OAuthStartService,
    YandexOAuthConfiguration,
    YandexOAuthIntegration,
    YandexOAuthProvider,
)
from app.modules.integrations.yandex.provider import (
    HttpxYandexOAuthProvider,
    YandexOAuthProviderFailure,
)
from app.modules.integrations.yandex.repository import ExternalIdentityConflict
from app.modules.sessions.cookies import SESSION_COOKIE_NAME, set_session_cookie
from app.modules.sessions.service import AuthenticatedSession, PostgresSessionService
from app.modules.tenancy.policy import AuthorizationDenied, Capability

router = APIRouter()


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
    if integration is None or integration.config is None:
        return _safe_error(request, status_code=503, code="oauth_unavailable")
    return integration, integration.config


def _provider(
    integration: YandexOAuthIntegration,
    config: YandexOAuthConfiguration,
) -> YandexOAuthProvider:
    return integration.provider or HttpxYandexOAuthProvider(config=config)


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
