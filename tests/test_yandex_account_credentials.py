from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from app.modules.integrations.yandex.credentials import (
    CredentialKeyRing,
    CredentialVault,
    CredentialVaultError,
    ProviderAccountLoginContext,
)


def test_vault_protects_provider_login_with_workspace_connection_and_account_context() -> None:
    login = "synthetic-provider-routing-login"
    vault = CredentialVault(
        CredentialKeyRing.from_keys(
            active_key_id="test-kek-v1",
            keys={"test-kek-v1": b"k" * 32},
        )
    )
    context = ProviderAccountLoginContext.for_yandex_provider_account(
        workspace_id=uuid4(),
        connection_id=uuid4(),
        provider_account_id=uuid4(),
    )

    encrypted = vault.encrypt_provider_account_login(context=context, login=login)

    assert encrypted.ciphertext != login.encode()
    assert login not in repr(encrypted)
    assert login not in repr(context)
    assert vault.decrypt_provider_account_login(context=context, encrypted=encrypted) == login


def test_vault_fails_closed_when_provider_login_account_context_changes() -> None:
    login = "synthetic-provider-routing-login"
    vault = CredentialVault(
        CredentialKeyRing.from_keys(
            active_key_id="test-kek-v1",
            keys={"test-kek-v1": b"k" * 32},
        )
    )
    context = ProviderAccountLoginContext.for_yandex_provider_account(
        workspace_id=uuid4(),
        connection_id=uuid4(),
        provider_account_id=uuid4(),
    )
    encrypted = vault.encrypt_provider_account_login(context=context, login=login)

    with pytest.raises(CredentialVaultError) as raised:
        vault.decrypt_provider_account_login(
            context=replace(context, provider_account_id=uuid4()),
            encrypted=encrypted,
        )

    assert login not in str(raised.value)
    assert login not in repr(raised.value)
