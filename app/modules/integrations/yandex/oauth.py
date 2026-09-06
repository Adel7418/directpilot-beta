from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from app.modules.integrations.yandex.credentials import (
    CredentialPayloadError,
    YandexCredentialPayload,
)

YANDEX_AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
LOCAL_YANDEX_CALLBACK_URI = "http://127.0.0.1:8000/api/v1/integrations/yandex/callback"
_PUBLIC_CALLBACK_PATH = "/api/v1/integrations/yandex/callback"
MAX_OAUTH_TRANSACTION_TTL = timedelta(minutes=10)
_ALLOWED_RETURN_PATHS = frozenset({"/"})


class OAuthConfigurationError(ValueError):
    """Raised when the configured OAuth callback URI is unsafe."""


def _is_supported_redirect_uri(redirect_uri: str) -> bool:
    if redirect_uri == LOCAL_YANDEX_CALLBACK_URI:
        return True
    try:
        parsed = urlsplit(redirect_uri)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and "?" not in redirect_uri
        and "#" not in redirect_uri
        and parsed.path == _PUBLIC_CALLBACK_PATH
        and (port is None or port > 0)
    )


@dataclass(frozen=True, slots=True)
class YandexOAuthConfiguration:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    redirect_uri: str
    transaction_ttl: timedelta = MAX_OAUTH_TRANSACTION_TTL

    def __post_init__(self) -> None:
        if (
            not isinstance(self.client_id, str)
            or not self.client_id.strip()
            or not isinstance(self.client_secret, str)
            or not self.client_secret.strip()
        ):
            raise OAuthConfigurationError("Yandex OAuth is not configured")
        if not _is_supported_redirect_uri(self.redirect_uri):
            raise OAuthConfigurationError("Yandex OAuth redirect URI is not configured")
        if not timedelta() < self.transaction_ttl <= MAX_OAUTH_TRANSACTION_TTL:
            raise OAuthConfigurationError("Yandex OAuth transaction TTL is not configured")


@dataclass(frozen=True, slots=True)
class NewOAuthTransaction:
    state_hash: str
    code_verifier: str = field(repr=False)
    user_id: UUID
    workspace_id: UUID
    browser_session_id: UUID
    return_path: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConsumedOAuthTransaction:
    id: UUID
    code_verifier: str = field(repr=False)
    user_id: UUID
    workspace_id: UUID
    browser_session_id: UUID
    return_path: str
    expires_at: datetime
    consumed_at: datetime


@dataclass(frozen=True, slots=True)
class OAuthStart:
    authorization_url: str = field(repr=False)
    expires_at: datetime


class OAuthTransactionWriter(Protocol):
    def create(self, transaction: NewOAuthTransaction) -> None: ...


class OAuthTransactionConsumer(Protocol):
    def consume(
        self,
        *,
        state: str,
        user_id: UUID,
        workspace_id: UUID,
        browser_session_id: UUID,
    ) -> ConsumedOAuthTransaction | None: ...


class ExternalIdentityBinder(Protocol):
    def bind_yandex_identity(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        issuer: str,
        subject: str,
        profile_login: str | None,
        profile_display_name: str | None,
    ) -> object: ...


class OAuthCredentialPersister(Protocol):
    def persist_yandex_oauth_tokens(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        issuer: str,
        subject: str,
        payload: YandexCredentialPayload,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class YandexOAuthTokenSet:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    token_type: str = "bearer"


@dataclass(frozen=True, slots=True)
class YandexUserInfo:
    subject: str = field(repr=False)
    client_id: str = field(repr=False)
    login: str | None = field(default=None, repr=False)
    display_name: str | None = field(default=None, repr=False)


class YandexOAuthProvider(Protocol):
    def exchange_code(self, *, code: str, code_verifier: str) -> YandexOAuthTokenSet: ...

    def fetch_user_info(self, *, access_token: str) -> YandexUserInfo: ...


class OAuthTransactionRepository(
    OAuthTransactionWriter,
    OAuthTransactionConsumer,
    Protocol,
):
    pass


@dataclass(frozen=True, slots=True)
class YandexOAuthIntegration:
    """Injectable OAuth seams; tests provide a config and mock provider."""

    transactions: OAuthTransactionRepository
    identities: ExternalIdentityBinder
    config: YandexOAuthConfiguration | None = None
    provider: YandexOAuthProvider | None = None
    credential_persister: OAuthCredentialPersister | None = None


class OAuthCallbackRejected(RuntimeError):
    """Raised for an invalid, expired, replayed, or browser-mismatched callback."""


class OAuthClientMismatch(RuntimeError):
    """Raised when userinfo does not belong to the configured OAuth application."""


class OAuthCredentialPersistenceUnavailable(RuntimeError):
    """Raised when OAuth callback vault persistence is not safely configured."""


@dataclass(frozen=True, slots=True)
class OAuthCallbackResult:
    return_path: str


def hash_oauth_state(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _pkce_s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _utc_now(value: datetime | None) -> datetime:
    now = datetime.now(timezone.utc) if value is None else value
    if now.tzinfo is None:
        raise ValueError("OAuth transaction time must be timezone-aware")
    return now.astimezone(timezone.utc)


def _validated_return_path(value: str) -> str:
    if value not in _ALLOWED_RETURN_PATHS:
        raise ValueError("OAuth return path is not allowed")
    return value


class OAuthStartService:
    """Create a short-lived, server-side Yandex OAuth authorization transaction."""

    def __init__(
        self,
        *,
        config: YandexOAuthConfiguration,
        transactions: OAuthTransactionWriter,
    ) -> None:
        self._config = config
        self._transactions = transactions

    def start(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        browser_session_id: UUID,
        return_path: str,
        now: datetime | None = None,
    ) -> OAuthStart:
        created_at = _utc_now(now)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(32)
        expires_at = created_at + self._config.transaction_ttl
        transaction = NewOAuthTransaction(
            state_hash=hash_oauth_state(state),
            code_verifier=verifier,
            user_id=user_id,
            workspace_id=workspace_id,
            browser_session_id=browser_session_id,
            return_path=_validated_return_path(return_path),
            created_at=created_at,
            expires_at=expires_at,
        )
        self._transactions.create(transaction)
        # The locally documented Yandex flow supports PKCE; DirectPilot requires S256 only.
        parameters = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "state": state,
            "code_challenge": _pkce_s256_challenge(verifier),
            "code_challenge_method": "S256",
        }
        authorization_url = f"{YANDEX_AUTHORIZE_URL}?{urlencode(parameters)}"
        return OAuthStart(authorization_url=authorization_url, expires_at=expires_at)


class OAuthCallbackService:
    """Spend an OAuth transaction before the mocked/injectable provider boundary."""

    def __init__(
        self,
        *,
        config: YandexOAuthConfiguration,
        transactions: OAuthTransactionConsumer,
        identities: ExternalIdentityBinder,
        provider: YandexOAuthProvider,
        credential_persister: OAuthCredentialPersister | None,
    ) -> None:
        self._config = config
        self._transactions = transactions
        self._identities = identities
        self._provider = provider
        self._credential_persister = credential_persister

    def complete(
        self,
        *,
        code: str,
        state: str,
        user_id: UUID,
        workspace_id: UUID,
        browser_session_id: UUID,
    ) -> OAuthCallbackResult:
        credential_persister = self._credential_persister
        if credential_persister is None:
            raise OAuthCredentialPersistenceUnavailable("OAuth credential vault is unavailable")
        if not _is_valid_callback_value(code, maximum_length=4096) or not _is_valid_callback_value(
            state, maximum_length=1024
        ):
            raise OAuthCallbackRejected("OAuth callback is invalid")
        transaction = self._transactions.consume(
            state=state,
            user_id=user_id,
            workspace_id=workspace_id,
            browser_session_id=browser_session_id,
        )
        if transaction is None:
            raise OAuthCallbackRejected("OAuth callback is unavailable")

        tokens = self._provider.exchange_code(
            code=code,
            code_verifier=transaction.code_verifier,
        )
        try:
            userinfo = self._provider.fetch_user_info(access_token=tokens.access_token)
            if not hmac.compare_digest(userinfo.client_id, self._config.client_id):
                raise OAuthClientMismatch("Yandex OAuth application is invalid")
            if not _is_valid_callback_value(userinfo.subject, maximum_length=255):
                raise OAuthCallbackRejected("Yandex OAuth subject is invalid")
            self._identities.bind_yandex_identity(
                user_id=transaction.user_id,
                workspace_id=transaction.workspace_id,
                issuer="https://login.yandex.ru",
                subject=userinfo.subject,
                profile_login=_bounded_profile_value(userinfo.login),
                profile_display_name=_bounded_profile_value(userinfo.display_name),
            )
            payload = _credential_payload_from_token_set(tokens)
            try:
                credential_persister.persist_yandex_oauth_tokens(
                    user_id=transaction.user_id,
                    workspace_id=transaction.workspace_id,
                    issuer="https://login.yandex.ru",
                    subject=userinfo.subject,
                    payload=payload,
                )
            finally:
                del payload
            return OAuthCallbackResult(return_path=transaction.return_path)
        finally:
            del tokens


def _credential_payload_from_token_set(tokens: YandexOAuthTokenSet) -> YandexCredentialPayload:
    if (
        not isinstance(tokens.expires_in, int)
        or isinstance(tokens.expires_in, bool)
        or tokens.expires_in <= 0
    ):
        raise OAuthCallbackRejected("OAuth token response is invalid")
    try:
        return YandexCredentialPayload(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            access_token_expires_at=_utc_now(None) + timedelta(seconds=tokens.expires_in),
            refresh_token_expires_at=None,
        )
    except CredentialPayloadError:
        raise OAuthCallbackRejected("OAuth token response is invalid") from None


def _is_valid_callback_value(value: str, *, maximum_length: int) -> bool:
    return bool(value) and value.isascii() and len(value) <= maximum_length


def _bounded_profile_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized[:255] or None
