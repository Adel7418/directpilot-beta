from __future__ import annotations

import secrets
from urllib.parse import parse_qs

import httpx
import pytest

from app.modules.integrations.yandex.oauth import YandexOAuthConfiguration
from app.modules.integrations.yandex.provider import (
    HttpxYandexOAuthProvider,
    YandexOAuthProviderFailure,
)


def test_provider_uses_basic_form_pkce_and_header_userinfo_without_network() -> None:
    config = YandexOAuthConfiguration(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        redirect_uri="http://127.0.0.1:8000/api/v1/integrations/yandex/callback",
    )
    code = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    access_token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(32)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "oauth.yandex.ru":
            form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
            assert request.method == "POST"
            assert request.url.path == "/token"
            assert request.headers["content-type"].startswith(
                "application/x-www-form-urlencoded"
            )
            assert request.headers["authorization"].startswith("Basic ")
            assert set(form) == {"grant_type", "code", "code_verifier"}
            return httpx.Response(
                200,
                json={
                    "token_type": "bearer",
                    "access_token": access_token,
                    "expires_in": 3600,
                    "refresh_token": refresh_token,
                },
            )
        assert request.method == "GET"
        assert request.url.host == "login.yandex.ru"
        assert request.url.path == "/info"
        assert dict(request.url.params) == {"format": "json"}
        assert request.headers["authorization"].startswith("OAuth ")
        return httpx.Response(
            200,
            json={
                "id": "stable-yandex-user-id",
                "client_id": config.client_id,
                "login": "synthetic-login",
                "display_name": "Synthetic display name",
            },
        )

    provider = HttpxYandexOAuthProvider(
        config=config,
        transport=httpx.MockTransport(handler),
    )

    tokens = provider.exchange_code(code=code, code_verifier=verifier)
    userinfo = provider.fetch_user_info(access_token=tokens.access_token)

    assert len(requests) == 2
    assert tokens.expires_in == 3600
    assert userinfo.subject == "stable-yandex-user-id"
    assert userinfo.login == "synthetic-login"
    assert "access_token=" not in repr(tokens)
    assert "refresh_token=" not in repr(tokens)


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    return any(marker in value for marker in markers)


def test_provider_maps_raw_error_body_to_a_safe_failure() -> None:
    config = YandexOAuthConfiguration(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        redirect_uri="http://127.0.0.1:8000/api/v1/integrations/yandex/callback",
    )
    raw_body_marker = secrets.token_urlsafe(32)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": raw_body_marker,
            },
        )

    provider = HttpxYandexOAuthProvider(
        config=config,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(YandexOAuthProviderFailure) as captured:
        provider.exchange_code(
            code=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(32),
        )

    assert captured.value.kind == "invalid_grant"
    assert not _contains_any(
        f"{captured.value!s}\n{captured.value!r}",
        (raw_body_marker, config.client_secret),
    )
