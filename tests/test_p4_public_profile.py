from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.responses import Response

from app.bootstrap import dependencies as dependencies_module
from app.bootstrap.application import create_app
from app.bootstrap.dependencies import (
    ApplicationDependencies,
    create_application_dependencies,
)
from app.bootstrap.public_profile import (
    PublicProfileConfigurationError,
    validate_public_profile_dependencies,
    validate_public_profile_static,
)
from app.config import RuntimeProfile, Settings, get_settings
from app.modules.integrations.yandex.client_factory import (
    PostgresConnectionScopedDirectClientFactory,
)
from app.modules.integrations.yandex.credentials import CredentialConfigurationError
from app.modules.integrations.yandex.oauth import (
    OAuthConfigurationError,
    YandexOAuthConfiguration,
    YandexOAuthIntegration,
)
from app.modules.integrations.yandex.provider import HttpxYandexOAuthProvider
from app.modules.integrations.yandex.refresh import YandexConnectionLifecycleService
from app.modules.sessions.cookies import (
    SESSION_COOKIE_POLICY,
    SessionCookiePolicy,
    clear_session_cookie,
    set_session_cookie,
)
from app.providers.yandex import DefaultDirectClientFactory, PublicDirectClientFactory


def _valid_public_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "runtime_profile": RuntimeProfile.PUBLIC,
        "public_base_url": "https://public.example.test",
        "yandex_oauth_redirect_uri": "https://public.example.test/api/v1/integrations/yandex/callback",
        "directpilot_mode": "live_readonly",
        "yandex_client_id": "synthetic-client-id",
        "yandex_client_secret": "synthetic-client-secret",
        "yandex_oauth_token": None,
        "yandex_metrika_oauth_token": None,
        "yandex_search_api_key": None,
        "credential_keyring_secret_file": "synthetic-keyring-path",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _assert_static_rejected(
    settings: Settings,
    *,
    expected_code: str,
    include_legacy_router: bool = False,
    fake_auth_enabled: bool = False,
    database_configured: bool = True,
    cookie_policy: SessionCookiePolicy | None = None,
) -> None:
    kwargs: dict[str, object] = {
        "include_legacy_router": include_legacy_router,
        "fake_auth_enabled": fake_auth_enabled,
        "database_configured": database_configured,
    }
    if cookie_policy is not None:
        kwargs["cookie_policy"] = cookie_policy
    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_static(settings, **kwargs)
    assert raised.value.code == expected_code


def test_valid_synthetic_public_settings_pass_static_validation() -> None:
    validate_public_profile_static(
        _valid_public_settings(),
        include_legacy_router=False,
        fake_auth_enabled=False,
        database_configured=True,
    )


def test_session_cookie_policy_drives_both_set_and_clear_operations() -> None:
    set_response = Response()
    clear_response = Response()
    set_session_cookie(
        set_response,
        token="synthetic-session-token",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    clear_session_cookie(clear_response)

    for response in (set_response, clear_response):
        parsed = SimpleCookie()
        parsed.load(response.headers["set-cookie"])
        morsel = parsed[SESSION_COOKIE_POLICY.name]
        assert morsel["path"] == SESSION_COOKIE_POLICY.path
        assert morsel["domain"] == ""
        assert morsel["secure"] is True
        assert morsel["httponly"] is True
        assert morsel["samesite"] == SESSION_COOKIE_POLICY.samesite


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    (
        ({"directpilot_mode": "sandbox"}, "public_directpilot_mode_invalid"),
        ({"yandex_client_id": None}, "public_oauth_client_id_required"),
        ({"yandex_client_secret": None}, "public_oauth_client_secret_required"),
        ({"credential_keyring_secret_file": None}, "public_credential_keyring_required"),
        ({"yandex_oauth_token": "synthetic-global-direct-token"}, "public_global_yandex_oauth_token_configured"),
        ({"yandex_metrika_oauth_token": "synthetic-global-metrika-token"}, "public_global_yandex_metrika_oauth_token_configured"),
        ({"yandex_search_api_key": "synthetic-global-search-key"}, "public_global_yandex_search_api_key_configured"),
    ),
)
def test_public_static_validation_rejects_each_non_url_setting_downgrade(
    overrides: dict[str, object],
    expected_code: str,
) -> None:
    settings = _valid_public_settings(**overrides)

    _assert_static_rejected(settings, expected_code=expected_code)


def test_public_static_validation_rejects_legacy_router_request() -> None:
    _assert_static_rejected(
        _valid_public_settings(),
        expected_code="public_legacy_router_enabled",
        include_legacy_router=True,
    )


def test_public_static_validation_rejects_fake_auth() -> None:
    _assert_static_rejected(
        _valid_public_settings(),
        expected_code="public_fake_auth_enabled",
        fake_auth_enabled=True,
    )


def test_public_static_validation_rejects_missing_database_configuration() -> None:
    _assert_static_rejected(
        _valid_public_settings(),
        expected_code="public_database_required",
        database_configured=False,
    )


@pytest.mark.parametrize(
    ("policy", "expected_code"),
    (
        (
            SessionCookiePolicy(
                name="directpilot_session",
                secure=True,
                httponly=True,
                samesite="lax",
                path="/",
                domain=None,
            ),
            "public_cookie_policy_invalid",
        ),
        (
            SessionCookiePolicy(
                name="__Host-directpilot_session",
                secure=False,
                httponly=True,
                samesite="lax",
                path="/",
                domain=None,
            ),
            "public_cookie_policy_invalid",
        ),
        (
            SessionCookiePolicy(
                name="__Host-directpilot_session",
                secure=True,
                httponly=False,
                samesite="lax",
                path="/",
                domain=None,
            ),
            "public_cookie_policy_invalid",
        ),
        (
            SessionCookiePolicy(
                name="__Host-directpilot_session",
                secure=True,
                httponly=True,
                samesite="none",
                path="/",
                domain=None,
            ),
            "public_cookie_policy_invalid",
        ),
        (
            SessionCookiePolicy(
                name="__Host-directpilot_session",
                secure=True,
                httponly=True,
                samesite="lax",
                path="/scope",
                domain=None,
            ),
            "public_cookie_policy_invalid",
        ),
        (
            SessionCookiePolicy(
                name="__Host-directpilot_session",
                secure=True,
                httponly=True,
                samesite="lax",
                path="/",
                domain="public.example.test",
            ),
            "public_cookie_policy_invalid",
        ),
    ),
)
def test_public_static_validation_rejects_each_cookie_policy_downgrade(
    policy: SessionCookiePolicy,
    expected_code: str,
) -> None:
    _assert_static_rejected(
        _valid_public_settings(),
        expected_code=expected_code,
        cookie_policy=policy,
    )


@pytest.mark.parametrize(
    ("public_base_url", "expected_code"),
    (
        (None, "public_base_url_required"),
        ("http://public.example.test", "public_base_url_invalid"),
        ("/relative", "public_base_url_invalid"),
        ("//public.example.test", "public_base_url_invalid"),
        ("https://user:pass@public.example.test", "public_base_url_invalid"),
        ("https://public.example.test?query=1", "public_base_url_invalid"),
        ("https://public.example.test#fragment", "public_base_url_invalid"),
        ("https:///missing-host", "public_base_url_invalid"),
    ),
)
def test_public_static_validation_rejects_invalid_public_base_url(
    public_base_url: str | None,
    expected_code: str,
) -> None:
    _assert_static_rejected(
        _valid_public_settings(public_base_url=public_base_url),
        expected_code=expected_code,
    )


@pytest.mark.parametrize(
    ("callback_url", "expected_code"),
    (
        ("http://public.example.test/api/v1/integrations/yandex/callback", "public_oauth_redirect_uri_invalid"),
        ("https://other.example.test/api/v1/integrations/yandex/callback", "public_oauth_callback_origin_mismatch"),
        ("https://public.example.test/wrong", "public_oauth_callback_path_invalid"),
        ("https://public.example.test/api/v1/integrations/yandex/callback?query=1", "public_oauth_redirect_uri_invalid"),
        ("https://public.example.test/api/v1/integrations/yandex/callback#fragment", "public_oauth_redirect_uri_invalid"),
    ),
)
def test_public_static_validation_rejects_invalid_oauth_callback(
    callback_url: str,
    expected_code: str,
) -> None:
    _assert_static_rejected(
        _valid_public_settings(yandex_oauth_redirect_uri=callback_url),
        expected_code=expected_code,
    )


def test_settings_profile_aliases_are_explicit_and_default_to_operator_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTPILOT_RUNTIME_PROFILE", "public")
    monkeypatch.setenv("DIRECTPILOT_PUBLIC_BASE_URL", "https://public.example.test")

    settings = Settings(_env_file=None)

    assert settings.runtime_profile is RuntimeProfile.PUBLIC
    assert settings.public_base_url == "https://public.example.test"
    assert Settings(_env_file=None, runtime_profile=RuntimeProfile.OPERATOR_LOCAL).runtime_profile is RuntimeProfile.OPERATOR_LOCAL


def test_settings_rejects_an_unknown_runtime_profile() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, runtime_profile="unrecognized")


def test_public_configuration_error_never_echoes_a_global_credential() -> None:
    marker = "synthetic-secret-value-that-must-not-appear"
    settings = _valid_public_settings(yandex_oauth_token=marker)

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_static(
            settings,
            include_legacy_router=False,
            fake_auth_enabled=False,
            database_configured=True,
        )

    assert marker not in str(raised.value)
    assert marker not in raised.value.message


class _CloseCountingRuntime:
    sessions = object()

    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _valid_public_dependencies() -> ApplicationDependencies:
    settings = _valid_public_settings()
    assert settings.yandex_client_id is not None
    assert settings.yandex_client_secret is not None
    oauth_config = YandexOAuthConfiguration(
        client_id=settings.yandex_client_id,
        client_secret=settings.yandex_client_secret,
        redirect_uri=settings.yandex_oauth_redirect_uri,
    )
    connection_repository = object()
    provider = HttpxYandexOAuthProvider(config=oauth_config)
    return ApplicationDependencies(
        repository=object(),
        direct_client_factory=PublicDirectClientFactory(),
        metrika_client_factory=object(),
        wordstat_client_factory=object(),
        connection_scoped_direct_client_factory=PostgresConnectionScopedDirectClientFactory(
            sessions=object(),
            vault=object(),
            workspace_authorizer=object(),
            settings=settings,
        ),
        database_runtime=_CloseCountingRuntime(),
        identity_repository=object(),
        session_service=object(),
        workspace_authorizer=object(),
        credential_vault=object(),
        yandex_oauth=YandexOAuthIntegration(
            transactions=object(),
            identities=object(),
            config=oauth_config,
            provider=provider,
            credential_persister=connection_repository,
        ),
        yandex_connection_lifecycle=YandexConnectionLifecycleService(
            repository=connection_repository,
            provider=provider,
        ),
    )


def test_valid_synthetic_public_dependencies_pass_post_validation() -> None:
    validate_public_profile_dependencies(_valid_public_settings(), _valid_public_dependencies())


def test_public_application_lifespan_closes_database_runtime_once() -> None:
    runtime = _CloseCountingRuntime()
    dependencies = replace(_valid_public_dependencies(), database_runtime=runtime)
    app = create_app(settings=_valid_public_settings(), dependencies=dependencies)

    with TestClient(app, base_url="https://public.example.test") as client:
        assert client.get("/health").status_code == 200

    assert runtime.close_calls == 1


def test_public_static_rejection_closes_explicit_database_runtime_once() -> None:
    runtime = _CloseCountingRuntime()
    dependencies = replace(_valid_public_dependencies(), database_runtime=runtime)

    with pytest.raises(PublicProfileConfigurationError):
        create_app(
            settings=_valid_public_settings(),
            dependencies=dependencies,
            include_legacy_router=True,
        )

    assert runtime.close_calls == 1


def test_public_dependency_rejection_closes_explicit_database_runtime_once() -> None:
    runtime = _CloseCountingRuntime()
    dependencies = replace(
        _valid_public_dependencies(),
        database_runtime=runtime,
        yandex_oauth=None,
    )

    with pytest.raises(PublicProfileConfigurationError):
        create_app(settings=_valid_public_settings(), dependencies=dependencies)

    assert runtime.close_calls == 1


def test_app_construction_failure_closes_database_runtime_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CloseCountingRuntime()
    dependencies = replace(_valid_public_dependencies(), database_runtime=runtime)

    def fail_fastapi(**_kwargs: object) -> None:
        raise RuntimeError("synthetic-app-construction-failure")

    monkeypatch.setattr("app.bootstrap.application.FastAPI", fail_fastapi)

    with pytest.raises(RuntimeError):
        create_app(settings=_valid_public_settings(), dependencies=dependencies)

    assert runtime.close_calls == 1


def test_public_post_validation_rejects_missing_yandex_oauth_integration() -> None:
    dependencies = replace(_valid_public_dependencies(), yandex_oauth=None)

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_dependencies(_valid_public_settings(), dependencies)

    assert raised.value.code == "public_yandex_oauth_required"


@pytest.mark.parametrize(
    "changes",
    (
        {"config": None},
        {"provider": None},
        {"credential_persister": None},
        {"transactions": None},
        {"identities": None},
    ),
)
def test_public_post_validation_rejects_each_missing_yandex_oauth_seam(
    changes: dict[str, object],
) -> None:
    dependencies = _valid_public_dependencies()
    integration = dependencies.yandex_oauth
    assert integration is not None
    dependencies = replace(dependencies, yandex_oauth=replace(integration, **changes))

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_dependencies(_valid_public_settings(), dependencies)

    assert raised.value.code == "public_yandex_oauth_dependencies_required"


def test_public_post_validation_rejects_missing_yandex_lifecycle() -> None:
    dependencies = replace(_valid_public_dependencies(), yandex_connection_lifecycle=None)

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_dependencies(_valid_public_settings(), dependencies)

    assert raised.value.code == "public_yandex_oauth_lifecycle_required"


@pytest.mark.parametrize("field", ("client_id", "client_secret", "redirect_uri"))
def test_public_post_validation_rejects_yandex_oauth_configuration_mismatch(
    field: str,
) -> None:
    dependencies = _valid_public_dependencies()
    integration = dependencies.yandex_oauth
    assert integration is not None
    config = integration.config
    assert config is not None
    marker = f"synthetic-mismatch-{field}"
    mismatched_config = YandexOAuthConfiguration(
        client_id=marker if field == "client_id" else config.client_id,
        client_secret=marker if field == "client_secret" else config.client_secret,
        redirect_uri=(
            "https://other.example.test/api/v1/integrations/yandex/callback"
            if field == "redirect_uri"
            else config.redirect_uri
        ),
    )
    dependencies = replace(
        dependencies,
        yandex_oauth=replace(integration, config=mismatched_config),
    )

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_dependencies(_valid_public_settings(), dependencies)

    assert raised.value.code == "public_yandex_oauth_configuration_mismatch"
    assert marker not in str(raised.value)


def test_public_post_validation_rejects_provider_configuration_mismatch() -> None:
    dependencies = _valid_public_dependencies()
    integration = dependencies.yandex_oauth
    assert integration is not None
    config = integration.config
    assert config is not None
    marker = "synthetic-provider-mismatch"
    provider = HttpxYandexOAuthProvider(
        config=YandexOAuthConfiguration(
            client_id=config.client_id,
            client_secret=config.client_secret,
            redirect_uri="https://other.example.test/api/v1/integrations/yandex/callback",
        )
    )
    dependencies = replace(
        dependencies,
        yandex_oauth=replace(integration, provider=provider),
    )

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_dependencies(_valid_public_settings(), dependencies)

    assert raised.value.code == "public_yandex_oauth_configuration_mismatch"
    assert marker not in str(raised.value)


def test_public_post_validation_rejects_non_concrete_yandex_oauth_provider() -> None:
    dependencies = _valid_public_dependencies()
    integration = dependencies.yandex_oauth
    assert integration is not None
    dependencies = replace(
        dependencies,
        yandex_oauth=replace(integration, provider=object()),
    )

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_dependencies(_valid_public_settings(), dependencies)

    assert raised.value.code == "public_yandex_oauth_provider_invalid"


@pytest.mark.parametrize("mismatch", ("provider", "credential_persister"))
def test_public_post_validation_rejects_yandex_lifecycle_graph_mismatch(
    mismatch: str,
) -> None:
    dependencies = _valid_public_dependencies()
    integration = dependencies.yandex_oauth
    assert integration is not None
    config = integration.config
    assert config is not None
    provider = (
        HttpxYandexOAuthProvider(config=config)
        if mismatch == "provider"
        else integration.provider
    )
    credential_persister = (
        object() if mismatch == "credential_persister" else integration.credential_persister
    )
    assert provider is not None
    assert credential_persister is not None
    dependencies = replace(
        dependencies,
        yandex_connection_lifecycle=YandexConnectionLifecycleService(
            repository=credential_persister,
            provider=provider,
        ),
    )

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_dependencies(_valid_public_settings(), dependencies)

    assert raised.value.code == "public_yandex_oauth_graph_mismatch"


def test_public_post_validation_rejects_non_concrete_yandex_lifecycle() -> None:
    dependencies = replace(_valid_public_dependencies(), yandex_connection_lifecycle=object())

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_dependencies(_valid_public_settings(), dependencies)

    assert raised.value.code == "public_yandex_oauth_lifecycle_invalid"


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    (
        ({"database_runtime": None}, "public_database_dependencies_required"),
        ({"identity_repository": None}, "public_database_dependencies_required"),
        ({"session_service": None}, "public_database_dependencies_required"),
        ({"workspace_authorizer": None}, "public_database_dependencies_required"),
        ({"credential_vault": None}, "public_connection_scoped_direct_factory_required"),
        ({"connection_scoped_direct_client_factory": None}, "public_connection_scoped_direct_factory_required"),
        ({"connection_scoped_direct_client_factory": object()}, "public_connection_scoped_direct_factory_required"),
        ({"fake_auth_enabled": True}, "public_fake_auth_enabled"),
        ({"direct_client_factory": DefaultDirectClientFactory()}, "public_direct_client_factory_unsafe"),
    ),
)
def test_public_post_validation_rejects_each_dependency_downgrade(
    changes: dict[str, object],
    expected_code: str,
) -> None:
    dependencies = replace(_valid_public_dependencies(), **changes)

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_dependencies(_valid_public_settings(), dependencies)

    assert raised.value.code == expected_code


def test_public_generic_direct_factory_never_creates_a_global_token_client() -> None:
    factory = PublicDirectClientFactory()

    assert factory.create(
        _valid_public_settings(yandex_oauth_token="synthetic-global-direct-token")
    ) is None


def test_create_application_dependencies_uses_the_explicit_settings_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, runtime_profile=RuntimeProfile.OPERATOR_LOCAL)
    monkeypatch.delenv(dependencies_module.DATABASE_URL_ENV, raising=False)
    monkeypatch.setattr(
        dependencies_module,
        "get_settings",
        lambda: pytest.fail("explicit Settings must be used"),
    )

    dependencies = create_application_dependencies(settings)

    assert isinstance(dependencies.direct_client_factory, DefaultDirectClientFactory)


def test_no_settings_bootstrap_resolves_current_production_profile_after_cache_warm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(dependencies_module.DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv("DIRECTPILOT_ENABLE_FAKE_AUTH", raising=False)
    for name in (
        "YANDEX_OAUTH_TOKEN",
        "YANDEX_METRIKA_OAUTH_TOKEN",
        "YANDEX_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DIRECTPILOT_APP_ENV", "local")
    monkeypatch.setenv("DIRECTPILOT_RUNTIME_PROFILE", "operator_local")
    get_settings.cache_clear()
    try:
        cached_settings = get_settings()
        assert cached_settings.app_env == "local"
        assert cached_settings.runtime_profile is RuntimeProfile.OPERATOR_LOCAL

        monkeypatch.setenv("DIRECTPILOT_APP_ENV", "production")
        monkeypatch.setenv("DIRECTPILOT_RUNTIME_PROFILE", "public")
        monkeypatch.setenv("DIRECTPILOT_PUBLIC_BASE_URL", "https://public.example.test")
        monkeypatch.setenv(
            "YANDEX_OAUTH_REDIRECT_URI",
            "https://public.example.test/api/v1/integrations/yandex/callback",
        )
        monkeypatch.setenv("YANDEX_CLIENT_ID", "synthetic-client-id")
        monkeypatch.setenv("YANDEX_CLIENT_SECRET", "synthetic-client-secret")
        monkeypatch.setenv("DIRECTPILOT_CREDENTIAL_KEYRING_SECRET_FILE", "synthetic-keyring-path")

        with pytest.raises(PublicProfileConfigurationError) as raised:
            create_application_dependencies()

        assert raised.value.code == "public_database_required"
    finally:
        get_settings.cache_clear()


def test_public_missing_database_fails_before_database_runtime_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _valid_public_settings()
    monkeypatch.delenv(dependencies_module.DATABASE_URL_ENV, raising=False)
    monkeypatch.setattr(
        dependencies_module,
        "create_database_runtime",
        lambda _settings: pytest.fail("database runtime must not be created"),
    )

    with pytest.raises(PublicProfileConfigurationError) as raised:
        create_application_dependencies(settings)

    assert raised.value.code == "public_database_required"


def test_public_post_validation_failure_closes_created_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        sessions = object()

        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    runtime = Runtime()
    settings = _valid_public_settings()
    monkeypatch.setenv(
        dependencies_module.DATABASE_URL_ENV,
        "postgresql+psycopg://app:synthetic@127.0.0.1:5432/directpilot",
    )
    monkeypatch.setattr(
        dependencies_module,
        "create_database_runtime",
        lambda _settings: runtime,
    )
    monkeypatch.setattr(dependencies_module, "check_schema_compatibility", lambda _runtime: None)
    monkeypatch.setattr(dependencies_module, "_configured_credential_vault", lambda _settings: object())

    def reject_post_validation(_settings: Settings, _dependencies: ApplicationDependencies) -> None:
        raise PublicProfileConfigurationError("public_direct_client_factory_unsafe")

    monkeypatch.setattr(
        dependencies_module,
        "validate_public_profile_dependencies",
        reject_post_validation,
    )

    with pytest.raises(PublicProfileConfigurationError):
        create_application_dependencies(settings)

    assert runtime.close_calls == 1


def test_public_unavailable_keyring_is_safe_and_prevents_database_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "synthetic-keyring-error-that-must-not-escape"
    monkeypatch.setenv(
        dependencies_module.DATABASE_URL_ENV,
        "postgresql+psycopg://app:synthetic@127.0.0.1:5432/directpilot",
    )
    monkeypatch.setattr(
        dependencies_module,
        "_configured_credential_vault",
        lambda _settings: (_ for _ in ()).throw(CredentialConfigurationError(marker)),
    )
    monkeypatch.setattr(
        dependencies_module,
        "create_database_runtime",
        lambda _settings: pytest.fail("database runtime must not be created"),
    )

    with pytest.raises(PublicProfileConfigurationError) as raised:
        create_application_dependencies(_valid_public_settings())

    assert raised.value.code == "public_credential_keyring_unavailable"
    assert marker not in str(raised.value)


def test_valid_synthetic_public_runtime_dependencies_are_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        sessions = object()

        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    runtime = Runtime()
    vault = object()
    monkeypatch.setenv(
        dependencies_module.DATABASE_URL_ENV,
        "postgresql+psycopg://app:synthetic@127.0.0.1:5432/directpilot",
    )
    monkeypatch.setattr(
        dependencies_module,
        "create_database_runtime",
        lambda _settings: runtime,
    )
    monkeypatch.setattr(dependencies_module, "check_schema_compatibility", lambda _runtime: None)
    monkeypatch.setattr(dependencies_module, "_configured_credential_vault", lambda _settings: vault)

    dependencies = create_application_dependencies(_valid_public_settings())

    assert isinstance(dependencies.direct_client_factory, PublicDirectClientFactory)
    assert isinstance(
        dependencies.connection_scoped_direct_client_factory,
        PostgresConnectionScopedDirectClientFactory,
    )
    assert dependencies.credential_vault is vault
    assert dependencies.fake_auth_enabled is False
    assert dependencies.database_runtime is runtime

    app = create_app(settings=_valid_public_settings(), dependencies=dependencies)

    with TestClient(app, base_url="https://public.example.test") as client:
        assert client.get("/health").status_code == 200

    assert runtime.close_calls == 1


def test_public_app_exposes_only_platform_health_and_oauth_routes() -> None:
    app = create_app(
        settings=_valid_public_settings(),
        dependencies=_valid_public_dependencies(),
    )
    paths = {route.path for route in app.routes}
    client = TestClient(app, base_url="https://public.example.test")

    assert {"/health", "/ready", "/api/v1/integrations/yandex/callback"} <= paths
    assert "/campaigns" not in paths
    assert "/api/v1/_test/identity/login" not in paths
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/api/v1/integrations/yandex/start").status_code == 401
    assert client.post("/campaign-drafts", json={}).status_code == 404
    assert client.post("/api/v1/_test/identity/login", json={}).status_code == 404
    for path in (
        "/campaigns",
        "/api/v1/_test/identity/login",
        "/docs",
        "/redoc",
        "/openapi.json",
    ):
        assert client.get(path).status_code == 404


def test_public_static_validation_rejects_explicit_https_port_zero() -> None:
    settings = _valid_public_settings(
        public_base_url="https://public.example.test:0",
        yandex_oauth_redirect_uri=(
            "https://public.example.test:0/api/v1/integrations/yandex/callback"
        ),
    )

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_static(
            settings,
            include_legacy_router=False,
            fake_auth_enabled=False,
            database_configured=True,
        )

    assert raised.value.code == "public_base_url_invalid"


@pytest.mark.parametrize(
    ("field", "expected_code"),
    (
        ("yandex_client_id", "public_oauth_client_id_required"),
        ("yandex_client_secret", "public_oauth_client_secret_required"),
    ),
)
def test_public_static_validation_rejects_whitespace_oauth_application_values(
    field: str,
    expected_code: str,
) -> None:
    _assert_static_rejected(
        _valid_public_settings(**{field: "   "}),
        expected_code=expected_code,
    )


@pytest.mark.parametrize("field", ("client_id", "client_secret", "redirect_uri"))
def test_yandex_oauth_configuration_rejects_unusable_public_values(field: str) -> None:
    marker = "synthetic-oauth-configuration-marker"
    with pytest.raises(OAuthConfigurationError) as raised:
        YandexOAuthConfiguration(
            client_id="   " if field == "client_id" else marker,
            client_secret="   " if field == "client_secret" else marker,
            redirect_uri=(
                "https://public.example.test:0/api/v1/integrations/yandex/callback"
                if field == "redirect_uri"
                else "https://public.example.test/api/v1/integrations/yandex/callback"
            ),
        )

    assert marker not in str(raised.value)


def test_create_app_uses_fresh_public_settings_after_operator_cache_warm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(dependencies_module.DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv("DIRECTPILOT_ENABLE_FAKE_AUTH", raising=False)
    for name in (
        "YANDEX_OAUTH_TOKEN",
        "YANDEX_METRIKA_OAUTH_TOKEN",
        "YANDEX_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DIRECTPILOT_APP_ENV", "local")
    monkeypatch.setenv("DIRECTPILOT_RUNTIME_PROFILE", "operator_local")
    get_settings.cache_clear()
    try:
        assert get_settings().runtime_profile is RuntimeProfile.OPERATOR_LOCAL
        monkeypatch.setenv("DIRECTPILOT_APP_ENV", "production")
        monkeypatch.setenv("DIRECTPILOT_RUNTIME_PROFILE", "public")
        monkeypatch.setenv("DIRECTPILOT_PUBLIC_BASE_URL", "https://public.example.test")
        monkeypatch.setenv(
            "YANDEX_OAUTH_REDIRECT_URI",
            "https://public.example.test/api/v1/integrations/yandex/callback",
        )
        monkeypatch.setenv("YANDEX_CLIENT_ID", "synthetic-client-id")
        monkeypatch.setenv("YANDEX_CLIENT_SECRET", "synthetic-client-secret")
        monkeypatch.setenv("DIRECTPILOT_CREDENTIAL_KEYRING_SECRET_FILE", "synthetic-keyring-path")

        app = create_app(dependencies=_valid_public_dependencies())

        assert app.docs_url is None
        assert app.redoc_url is None
        assert app.openapi_url is None
        paths = {route.path for route in app.routes}
        assert "/campaigns" not in paths
        assert "/api/v1/_test/identity/login" not in paths
    finally:
        get_settings.cache_clear()


def test_main_uses_fresh_public_settings_after_operator_cache_warm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(dependencies_module.DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv("DIRECTPILOT_ENABLE_FAKE_AUTH", raising=False)
    for name in (
        "YANDEX_OAUTH_TOKEN",
        "YANDEX_METRIKA_OAUTH_TOKEN",
        "YANDEX_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DIRECTPILOT_APP_ENV", "local")
    monkeypatch.setenv("DIRECTPILOT_RUNTIME_PROFILE", "operator_local")
    app_package = importlib.import_module("app")
    package_main_was_bound = "main" in app_package.__dict__
    previous_package_main = app_package.__dict__.get("main")
    previous_main = sys.modules.pop("app.main", None)
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.bootstrap.application.create_application_dependencies",
        lambda _settings, *, include_legacy_router: _valid_public_dependencies(),
    )
    try:
        assert get_settings().runtime_profile is RuntimeProfile.OPERATOR_LOCAL
        monkeypatch.setenv("DIRECTPILOT_APP_ENV", "production")
        monkeypatch.setenv("DIRECTPILOT_RUNTIME_PROFILE", "public")
        monkeypatch.setenv("DIRECTPILOT_PUBLIC_BASE_URL", "https://public.example.test")
        monkeypatch.setenv(
            "YANDEX_OAUTH_REDIRECT_URI",
            "https://public.example.test/api/v1/integrations/yandex/callback",
        )
        monkeypatch.setenv("YANDEX_CLIENT_ID", "synthetic-client-id")
        monkeypatch.setenv("YANDEX_CLIENT_SECRET", "synthetic-client-secret")
        monkeypatch.setenv("DIRECTPILOT_CREDENTIAL_KEYRING_SECRET_FILE", "synthetic-keyring-path")

        app = importlib.import_module("app.main").app

        assert app.docs_url is None
        assert app.redoc_url is None
        assert app.openapi_url is None
        paths = {route.path for route in app.routes}
        assert "/campaigns" not in paths
        assert "/api/v1/_test/identity/login" not in paths
    finally:
        sys.modules.pop("app.main", None)
        if previous_main is not None:
            sys.modules["app.main"] = previous_main
        if package_main_was_bound:
            app_package.__dict__["main"] = previous_package_main
        else:
            app_package.__dict__.pop("main", None)
        get_settings.cache_clear()


def test_public_health_does_not_expose_configuration_or_credentials() -> None:
    marker = "synthetic-secret-value-that-must-not-appear"
    app = create_app(
        settings=_valid_public_settings(),
        dependencies=_valid_public_dependencies(),
    )

    response = TestClient(app, base_url="https://public.example.test").get("/health")

    assert response.status_code == 200
    assert marker not in response.text
    assert "token" not in response.text.lower()
    assert "credential" not in response.text.lower()


def test_public_app_rejects_legacy_router_before_returning_an_asgi_application() -> None:
    with pytest.raises(PublicProfileConfigurationError) as raised:
        create_app(
            settings=_valid_public_settings(),
            dependencies=_valid_public_dependencies(),
            include_legacy_router=True,
        )

    assert raised.value.code == "public_legacy_router_enabled"


def test_operator_application_factory_keeps_existing_identity_and_docs_wiring() -> None:
    settings = Settings(_env_file=None, runtime_profile=RuntimeProfile.OPERATOR_LOCAL)
    dependencies = create_application_dependencies(settings)

    app = create_app(settings=settings, dependencies=dependencies)
    paths = {route.path for route in app.routes}

    assert "/api/v1/_test/identity/login" in paths
    assert "/api/v1/integrations/yandex/callback" in paths
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"


def test_public_fake_auth_environment_is_rejected_before_database_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTPILOT_ENABLE_FAKE_AUTH", "1")
    monkeypatch.delenv(dependencies_module.DATABASE_URL_ENV, raising=False)

    with pytest.raises(PublicProfileConfigurationError) as raised:
        create_application_dependencies(_valid_public_settings())

    assert raised.value.code == "public_fake_auth_enabled"


def test_public_fake_auth_environment_is_rejected_even_when_app_env_is_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTPILOT_ENABLE_FAKE_AUTH", "1")
    monkeypatch.delenv(dependencies_module.DATABASE_URL_ENV, raising=False)

    with pytest.raises(PublicProfileConfigurationError) as raised:
        create_application_dependencies(_valid_public_settings(app_env="production"))

    assert raised.value.code == "public_fake_auth_enabled"


def test_public_global_oauth_environment_credential_is_rejected_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "synthetic-global-token-from-environment"
    monkeypatch.setenv("YANDEX_OAUTH_TOKEN", marker)
    settings = Settings(
        _env_file=None,
        runtime_profile=RuntimeProfile.PUBLIC,
        public_base_url="https://public.example.test",
        yandex_oauth_redirect_uri="https://public.example.test/api/v1/integrations/yandex/callback",
        directpilot_mode="live_readonly",
        yandex_client_id="synthetic-client-id",
        yandex_client_secret="synthetic-client-secret",
        credential_keyring_secret_file="synthetic-keyring-path",
    )

    with pytest.raises(PublicProfileConfigurationError) as raised:
        validate_public_profile_static(
            settings,
            include_legacy_router=False,
            fake_auth_enabled=False,
            database_configured=True,
        )

    assert raised.value.code == "public_global_yandex_oauth_token_configured"
    assert marker not in str(raised.value)


def test_public_startup_rejection_does_not_log_a_configured_credential(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "synthetic-secret-value-that-must-not-appear-in-logs"
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(
        dependencies_module.DATABASE_URL_ENV,
        "postgresql+psycopg://app:synthetic@127.0.0.1:5432/directpilot",
    )

    with pytest.raises(PublicProfileConfigurationError) as raised:
        create_application_dependencies(_valid_public_settings(yandex_oauth_token=marker))

    assert raised.value.code == "public_global_yandex_oauth_token_configured"
    assert marker not in str(raised.value)
    assert marker not in caplog.text


def test_public_main_entrypoint_never_imports_legacy_or_identity_test_router(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import app.bootstrap.application as application_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(dependencies_module.DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv("DIRECTPILOT_ENABLE_FAKE_AUTH", raising=False)
    for name in (
        "YANDEX_OAUTH_TOKEN",
        "YANDEX_METRIKA_OAUTH_TOKEN",
        "YANDEX_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DIRECTPILOT_APP_ENV", "production")
    monkeypatch.setenv("DIRECTPILOT_RUNTIME_PROFILE", "public")
    monkeypatch.setenv("DIRECTPILOT_PUBLIC_BASE_URL", "https://public.example.test")
    monkeypatch.setenv(
        "YANDEX_OAUTH_REDIRECT_URI",
        "https://public.example.test/api/v1/integrations/yandex/callback",
    )
    monkeypatch.setenv("YANDEX_CLIENT_ID", "synthetic-client-id")
    monkeypatch.setenv("YANDEX_CLIENT_SECRET", "synthetic-client-secret")
    monkeypatch.setenv("DIRECTPILOT_CREDENTIAL_KEYRING_SECRET_FILE", "synthetic-keyring-path")
    dependencies = _valid_public_dependencies()
    monkeypatch.setattr(
        application_module,
        "create_application_dependencies",
        lambda configured_settings, *, include_legacy_router: dependencies,
    )
    app_package = importlib.import_module("app")
    original_main = sys.modules.get("app.main")
    package_main_was_bound = "main" in app_package.__dict__
    original_package_main = app_package.__dict__.get("main")
    monkeypatch.delitem(sys.modules, "app.main", raising=False)
    monkeypatch.delitem(sys.modules, "app.api.legacy_router", raising=False)
    monkeypatch.delitem(sys.modules, "app.modules.identity.router", raising=False)

    try:
        main = importlib.import_module("app.main")

        assert main.app.state.dependencies is dependencies
        assert main.__all__ == ["app"]
        assert "app.api.legacy_router" not in sys.modules
        assert "app.modules.identity.router" not in sys.modules
    finally:
        if original_main is None:
            sys.modules.pop("app.main", None)
        else:
            sys.modules["app.main"] = original_main
        if package_main_was_bound:
            app_package.__dict__["main"] = original_package_main
        else:
            app_package.__dict__.pop("main", None)

    if original_main is None:
        assert "app.main" not in sys.modules
    else:
        assert sys.modules["app.main"] is original_main
    if package_main_was_bound:
        assert app_package.__dict__["main"] is original_package_main
    else:
        assert "main" not in app_package.__dict__
