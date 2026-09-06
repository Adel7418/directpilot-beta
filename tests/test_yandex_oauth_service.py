from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from app.modules.integrations.yandex.credentials import YandexCredentialPayload
from app.modules.integrations.yandex.oauth import (
    ConsumedOAuthTransaction,
    NewOAuthTransaction,
    OAuthCallbackService,
    OAuthStartService,
    YandexOAuthConfiguration,
    YandexOAuthTokenSet,
    YandexUserInfo,
    hash_oauth_state,
)


class _RecordingOAuthTransactions:
    def __init__(self) -> None:
        self.created: NewOAuthTransaction | None = None

    def create(self, transaction: NewOAuthTransaction) -> None:
        self.created = transaction


def test_start_builds_the_exact_yandex_authorize_url_with_s256_pkce() -> None:
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    transactions = _RecordingOAuthTransactions()
    config = YandexOAuthConfiguration(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        redirect_uri="http://127.0.0.1:8000/api/v1/integrations/yandex/callback",
    )
    service = OAuthStartService(config=config, transactions=transactions)

    started = service.start(
        user_id=uuid4(),
        workspace_id=uuid4(),
        browser_session_id=uuid4(),
        return_path="/",
        now=now,
    )

    transaction = transactions.created
    assert transaction is not None
    query = parse_qs(urlsplit(started.authorization_url).query, strict_parsing=True)
    assert urlsplit(started.authorization_url).scheme == "https"
    assert urlsplit(started.authorization_url).netloc == "oauth.yandex.ru"
    assert urlsplit(started.authorization_url).path == "/authorize"
    assert set(query) == {
        "response_type",
        "client_id",
        "redirect_uri",
        "state",
        "code_challenge",
        "code_challenge_method",
    }
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [config.client_id]
    assert query["redirect_uri"] == [config.redirect_uri]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["state"][0]) >= 43
    assert hmac.compare_digest(transaction.state_hash, hash_oauth_state(query["state"][0]))
    assert transaction.expires_at == now + timedelta(minutes=10)
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(transaction.code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert hmac.compare_digest(query["code_challenge"][0], expected_challenge)
    assert "code_verifier=" not in repr(transaction)


class _ConsumedOAuthTransactions:
    def __init__(self, transaction: ConsumedOAuthTransaction) -> None:
        self._transaction = transaction
        self.calls = 0

    def consume(self, **_: object) -> ConsumedOAuthTransaction | None:
        self.calls += 1
        return self._transaction


class _RecordingIdentities:
    def __init__(self) -> None:
        self.user_id: UUID | None = None
        self.workspace_id: UUID | None = None
        self.subject: str | None = None

    def bind_yandex_identity(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        issuer: str,
        subject: str,
        profile_login: str | None,
        profile_display_name: str | None,
    ) -> None:
        assert issuer == "https://login.yandex.ru"
        assert profile_login == "synthetic-login"
        assert profile_display_name == "Synthetic display name"
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.subject = subject


class _RecordingCredentialPersister:
    def __init__(self) -> None:
        self.calls = 0
        self.user_id: UUID | None = None
        self.workspace_id: UUID | None = None
        self.subject: str | None = None

    def persist_yandex_oauth_tokens(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        issuer: str,
        subject: str,
        payload: YandexCredentialPayload,
    ) -> None:
        assert issuer == "https://login.yandex.ru"
        assert payload.access_token_expires_at > datetime.now(timezone.utc)
        self.calls += 1
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.subject = subject


class _MockYandexOAuthProvider:
    def __init__(self, config: YandexOAuthConfiguration) -> None:
        self._config = config
        self.code: str | None = None
        self.verifier: str | None = None
        self.userinfo_calls = 0

    def exchange_code(self, *, code: str, code_verifier: str) -> YandexOAuthTokenSet:
        self.code = code
        self.verifier = code_verifier
        return YandexOAuthTokenSet(
            access_token=secrets.token_urlsafe(32),
            refresh_token=secrets.token_urlsafe(32),
            expires_in=3600,
        )

    def fetch_user_info(self, *, access_token: str) -> YandexUserInfo:
        assert len(access_token) >= 43
        self.userinfo_calls += 1
        return YandexUserInfo(
            subject="stable-yandex-user-id",
            client_id=self._config.client_id,
            login="synthetic-login",
            display_name="Synthetic display name",
        )


def test_callback_binds_the_stable_yandex_subject_to_the_authenticated_local_user() -> None:
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    user_id = uuid4()
    workspace_id = uuid4()
    browser_session_id = uuid4()
    code = secrets.token_urlsafe(32)
    config = YandexOAuthConfiguration(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        redirect_uri="http://127.0.0.1:8000/api/v1/integrations/yandex/callback",
    )
    transactions = _ConsumedOAuthTransactions(
        ConsumedOAuthTransaction(
            id=uuid4(),
            code_verifier=secrets.token_urlsafe(32),
            user_id=user_id,
            workspace_id=workspace_id,
            browser_session_id=browser_session_id,
            return_path="/",
            expires_at=now + timedelta(minutes=5),
            consumed_at=now,
        )
    )
    identities = _RecordingIdentities()
    provider = _MockYandexOAuthProvider(config)
    credential_persister = _RecordingCredentialPersister()
    service = OAuthCallbackService(
        config=config,
        transactions=transactions,
        identities=identities,
        provider=provider,
        credential_persister=credential_persister,
    )

    completed = service.complete(
        code=code,
        state=secrets.token_urlsafe(32),
        user_id=user_id,
        workspace_id=workspace_id,
        browser_session_id=browser_session_id,
    )

    assert transactions.calls == 1
    assert provider.code is not None and hmac.compare_digest(provider.code, code)
    assert provider.verifier is not None
    assert identities.user_id == user_id
    assert identities.workspace_id == workspace_id
    assert identities.subject == "stable-yandex-user-id"
    assert credential_persister.calls == 1
    assert credential_persister.user_id == user_id
    assert credential_persister.workspace_id == workspace_id
    assert credential_persister.subject == "stable-yandex-user-id"
    assert provider.userinfo_calls == 1
    assert completed.return_path == "/"
