from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import urlsplit

from app.config import RuntimeProfile, Settings
from app.modules.integrations.yandex.client_factory import (
    PostgresConnectionScopedDirectClientFactory,
)
from app.modules.integrations.yandex.oauth import YandexOAuthConfiguration
from app.modules.integrations.yandex.provider import HttpxYandexOAuthProvider
from app.modules.integrations.yandex.refresh import YandexConnectionLifecycleService
from app.modules.sessions.cookies import SESSION_COOKIE_POLICY, SessionCookiePolicy

if TYPE_CHECKING:
    from app.bootstrap.dependencies import ApplicationDependencies


_EXPECTED_OAUTH_CALLBACK_PATH = "/api/v1/integrations/yandex/callback"
_SAFE_MESSAGE = "Public profile configuration is invalid."


class PublicProfileConfigurationError(RuntimeError):
    """Safe startup error for public runtime configuration failures."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.message = _SAFE_MESSAGE
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class _ParsedHttpsUrl:
    origin: tuple[str, str, int]
    path: str


def _public_error(code: str) -> NoReturn:
    raise PublicProfileConfigurationError(code)


def _parse_https_url(value: str | None) -> _ParsedHttpsUrl | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        return None
    if parsed.netloc.endswith(":") or port == 0:
        return None
    return _ParsedHttpsUrl(
        origin=("https", parsed.hostname.lower(), 443 if port is None else port),
        path=parsed.path,
    )


def _public_cookie_policy_is_safe(policy: SessionCookiePolicy) -> bool:
    return policy == SESSION_COOKIE_POLICY


def _is_configured(value: str | None) -> bool:
    return value is not None and value != ""


def _is_nonblank(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _oauth_configuration_matches_settings(
    config: YandexOAuthConfiguration,
    settings: Settings,
) -> bool:
    return (
        config.client_id == settings.yandex_client_id
        and config.client_secret == settings.yandex_client_secret
        and config.redirect_uri == settings.yandex_oauth_redirect_uri
    )


def validate_public_profile_static(
    settings: Settings,
    *,
    include_legacy_router: bool,
    fake_auth_enabled: bool,
    database_configured: bool,
    cookie_policy: SessionCookiePolicy = SESSION_COOKIE_POLICY,
) -> None:
    """Validate public settings without touching database or ASGI resources."""

    if settings.runtime_profile is not RuntimeProfile.PUBLIC:
        return
    if include_legacy_router:
        _public_error("public_legacy_router_enabled")
    if fake_auth_enabled:
        _public_error("public_fake_auth_enabled")
    if not _public_cookie_policy_is_safe(cookie_policy):
        _public_error("public_cookie_policy_invalid")

    public_base_url = _parse_https_url(settings.public_base_url)
    if settings.public_base_url is None or settings.public_base_url == "":
        _public_error("public_base_url_required")
    if public_base_url is None:
        _public_error("public_base_url_invalid")

    callback_url = _parse_https_url(settings.yandex_oauth_redirect_uri)
    if callback_url is None:
        _public_error("public_oauth_redirect_uri_invalid")
    if callback_url.origin != public_base_url.origin:
        _public_error("public_oauth_callback_origin_mismatch")
    if callback_url.path != _EXPECTED_OAUTH_CALLBACK_PATH:
        _public_error("public_oauth_callback_path_invalid")

    if settings.directpilot_mode != "live_readonly":
        _public_error("public_directpilot_mode_invalid")
    if not database_configured:
        _public_error("public_database_required")
    if not _is_nonblank(settings.yandex_client_id):
        _public_error("public_oauth_client_id_required")
    if not _is_nonblank(settings.yandex_client_secret):
        _public_error("public_oauth_client_secret_required")
    if not _is_configured(settings.credential_keyring_secret_file):
        _public_error("public_credential_keyring_required")

    for value, code in (
        (settings.yandex_oauth_token, "public_global_yandex_oauth_token_configured"),
        (settings.yandex_metrika_oauth_token, "public_global_yandex_metrika_oauth_token_configured"),
        (settings.yandex_search_api_key, "public_global_yandex_search_api_key_configured"),
    ):
        if _is_configured(value):
            _public_error(code)


def validate_public_profile_dependencies(
    settings: Settings,
    dependencies: ApplicationDependencies,
) -> None:
    """Validate public dependency wiring after database construction."""

    if settings.runtime_profile is not RuntimeProfile.PUBLIC:
        return
    if (
        dependencies.database_runtime is None
        or dependencies.identity_repository is None
        or dependencies.session_service is None
        or dependencies.workspace_authorizer is None
    ):
        _public_error("public_database_dependencies_required")
    if (
        getattr(dependencies, "credential_vault", None) is None
        or not isinstance(
            dependencies.connection_scoped_direct_client_factory,
            PostgresConnectionScopedDirectClientFactory,
        )
    ):
        _public_error("public_connection_scoped_direct_factory_required")
    if dependencies.fake_auth_enabled:
        _public_error("public_fake_auth_enabled")

    from app.providers.yandex import PublicDirectClientFactory

    if not isinstance(dependencies.direct_client_factory, PublicDirectClientFactory):
        _public_error("public_direct_client_factory_unsafe")
    yandex_oauth = dependencies.yandex_oauth
    if yandex_oauth is None:
        _public_error("public_yandex_oauth_required")
    if (
        yandex_oauth.config is None
        or yandex_oauth.provider is None
        or yandex_oauth.credential_persister is None
        or yandex_oauth.transactions is None
        or yandex_oauth.identities is None
    ):
        _public_error("public_yandex_oauth_dependencies_required")
    if not _oauth_configuration_matches_settings(yandex_oauth.config, settings):
        _public_error("public_yandex_oauth_configuration_mismatch")
    if not isinstance(yandex_oauth.provider, HttpxYandexOAuthProvider):
        _public_error("public_yandex_oauth_provider_invalid")
    if not _oauth_configuration_matches_settings(yandex_oauth.provider.config, settings):
        _public_error("public_yandex_oauth_configuration_mismatch")
    lifecycle = dependencies.yandex_connection_lifecycle
    if lifecycle is None:
        _public_error("public_yandex_oauth_lifecycle_required")
    if not isinstance(lifecycle, YandexConnectionLifecycleService):
        _public_error("public_yandex_oauth_lifecycle_invalid")
    if (
        lifecycle.provider is not yandex_oauth.provider
        or lifecycle.repository is not yandex_oauth.credential_persister
    ):
        _public_error("public_yandex_oauth_graph_mismatch")
