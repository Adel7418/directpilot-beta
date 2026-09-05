from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from app.modules.integrations.yandex.oauth import (
    YandexOAuthConfiguration,
    YandexOAuthTokenSet,
    YandexUserInfo,
)

_TOKEN_URL = "https://oauth.yandex.ru/token"
_USERINFO_URL = "https://login.yandex.ru/info"
_KNOWN_TOKEN_ERRORS = frozenset(
    {
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_scope",
        "unauthorized_client",
        "unsupported_grant_type",
    }
)


class YandexOAuthProviderFailure(RuntimeError):
    """A safe provider outcome that intentionally omits upstream body content."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__("Yandex OAuth provider request failed")


class HttpxYandexOAuthProvider:
    """Synchronous HTTP boundary for Yandex OAuth; all tests inject a mock transport."""

    def __init__(
        self,
        *,
        config: YandexOAuthConfiguration,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport

    def exchange_code(self, *, code: str, code_verifier: str) -> YandexOAuthTokenSet:
        try:
            with httpx.Client(
                timeout=httpx.Timeout(10.0),
                transport=self._transport,
            ) as client:
                response = client.post(
                    _TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "code_verifier": code_verifier,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    auth=(self._config.client_id, self._config.client_secret),
                )
        except httpx.HTTPError:
            raise YandexOAuthProviderFailure("provider_unavailable") from None
        if response.is_error:
            raise _token_failure(response)
        payload = _json_mapping(response, kind="provider_response_invalid")
        token_type = payload.get("token_type")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or not isinstance(access_token, str)
            or not access_token
            or not isinstance(refresh_token, str)
            or not refresh_token
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise YandexOAuthProviderFailure("provider_response_invalid")
        return YandexOAuthTokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            token_type=token_type.lower(),
        )

    def fetch_user_info(self, *, access_token: str) -> YandexUserInfo:
        try:
            with httpx.Client(
                timeout=httpx.Timeout(10.0),
                transport=self._transport,
            ) as client:
                response = client.get(
                    _USERINFO_URL,
                    params={"format": "json"},
                    headers={"Authorization": f"OAuth {access_token}"},
                )
        except httpx.HTTPError:
            raise YandexOAuthProviderFailure("provider_unavailable") from None
        if response.is_error:
            raise YandexOAuthProviderFailure("userinfo_failed")
        payload = _json_mapping(response, kind="userinfo_invalid")
        subject = payload.get("id")
        client_id = payload.get("client_id")
        if not isinstance(subject, str) or not subject or not isinstance(client_id, str) or not client_id:
            raise YandexOAuthProviderFailure("userinfo_invalid")
        return YandexUserInfo(
            subject=subject,
            client_id=client_id,
            login=_optional_text(payload.get("login")),
            display_name=_profile_display_name(payload),
        )


def _json_mapping(response: httpx.Response, *, kind: str) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise YandexOAuthProviderFailure(kind) from None
    if not isinstance(payload, Mapping):
        raise YandexOAuthProviderFailure(kind)
    return payload


def _token_failure(response: httpx.Response) -> YandexOAuthProviderFailure:
    try:
        payload = response.json()
    except ValueError:
        return YandexOAuthProviderFailure("provider_error")
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if isinstance(error, str) and error in _KNOWN_TOKEN_ERRORS:
        return YandexOAuthProviderFailure(error)
    return YandexOAuthProviderFailure("provider_error")


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _profile_display_name(payload: Mapping[str, Any]) -> str | None:
    for field in ("display_name", "real_name"):
        value = _optional_text(payload.get(field))
        if value is not None:
            return value
    return None
