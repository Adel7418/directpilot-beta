from __future__ import annotations

import base64
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.modules.integrations.yandex.credentials import (
    CredentialConfigurationError,
    CredentialContext,
    CredentialKeyRing,
    CredentialVault,
    CredentialVaultError,
    YandexCredentialPayload,
)


def test_vault_round_trip_returns_token_and_expiry_metadata_without_secret_repr() -> None:
    key_ring = CredentialKeyRing.from_keys(
        active_key_id="test-kek-v1",
        keys={"test-kek-v1": b"k" * 32},
    )
    vault = CredentialVault(key_ring)
    context = CredentialContext.for_yandex_connection(
        workspace_id=uuid4(),
        connection_id=uuid4(),
    )
    payload = YandexCredentialPayload(
        access_token="synthetic-access-token",
        refresh_token="synthetic-refresh-token",
        access_token_expires_at=datetime(2026, 9, 6, 13, tzinfo=timezone.utc),
        refresh_token_expires_at=datetime(2026, 10, 6, 13, tzinfo=timezone.utc),
    )

    encrypted = vault.encrypt(context=context, payload=payload)
    decrypted = vault.decrypt(context=context, encrypted=encrypted)

    assert decrypted == payload
    assert encrypted.kek_key_id == "test-kek-v1"
    assert len(encrypted.token_nonce) == 12
    assert len(encrypted.wrap_nonce) == 12
    assert encrypted.token_ciphertext != payload.access_token.encode()
    for protected_value in (payload.access_token, payload.refresh_token):
        assert protected_value not in repr(payload)
        assert protected_value not in repr(encrypted)
        assert protected_value not in repr(decrypted)


def test_key_ring_loads_unpadded_base64url_keys_from_a_private_secret_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "credential-keyring.json"
    encoded_key = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode("ascii")
    secret_file.write_text(
        json.dumps(
            {
                "active_key_id": "test-kek-v1",
                "keys": {"test-kek-v1": encoded_key},
            }
        ),
        encoding="utf-8",
    )
    secret_file.chmod(0o600)

    key_ring = CredentialKeyRing.from_json_secret_file(secret_file)

    assert key_ring.active_key_id == "test-kek-v1"
    assert "s" * 32 not in repr(key_ring)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are unavailable")
@pytest.mark.parametrize("mode", [0o640, 0o644])
def test_key_ring_rejects_group_or_world_readable_secret_files(tmp_path: Path, mode: int) -> None:
    secret_file = tmp_path / "credential-keyring.json"
    secret_file.write_text('{"active_key_id":"test-kek-v1","keys":{}}', encoding="utf-8")
    secret_file.chmod(mode)

    with pytest.raises(CredentialConfigurationError):
        CredentialKeyRing.from_json_secret_file(secret_file)


def _credential_payload() -> YandexCredentialPayload:
    return YandexCredentialPayload(
        access_token="synthetic-access-token",
        refresh_token="synthetic-refresh-token",
        access_token_expires_at=datetime(2026, 9, 6, 13, tzinfo=timezone.utc),
        refresh_token_expires_at=datetime(2026, 10, 6, 13, tzinfo=timezone.utc),
    )


def _flip_byte(value: bytes) -> bytes:
    return bytes([value[0] ^ 1]) + value[1:]


@pytest.mark.parametrize(
    "tamper_target",
    [
        "token_ciphertext",
        "token_nonce",
        "wrapped_dek",
        "wrap_nonce",
        "kek_key_id",
        "workspace_id",
        "connection_id",
        "token_purpose",
        "wrap_purpose",
    ],
)
def test_vault_fails_closed_for_every_authenticated_envelope_binding(
    tamper_target: str,
) -> None:
    key_ring = CredentialKeyRing.from_keys(
        active_key_id="test-kek-v1",
        keys={"test-kek-v1": b"k" * 32, "test-kek-v2": b"v" * 32},
    )
    vault = CredentialVault(key_ring)
    context = CredentialContext.for_yandex_connection(
        workspace_id=uuid4(),
        connection_id=uuid4(),
    )
    encrypted = vault.encrypt(context=context, payload=_credential_payload())
    tampered_context = context
    tampered_encrypted = encrypted
    if tamper_target == "token_ciphertext":
        tampered_encrypted = replace(
            encrypted,
            token_ciphertext=_flip_byte(encrypted.token_ciphertext),
        )
    elif tamper_target == "token_nonce":
        tampered_encrypted = replace(encrypted, token_nonce=_flip_byte(encrypted.token_nonce))
    elif tamper_target == "wrapped_dek":
        tampered_encrypted = replace(encrypted, wrapped_dek=_flip_byte(encrypted.wrapped_dek))
    elif tamper_target == "wrap_nonce":
        tampered_encrypted = replace(encrypted, wrap_nonce=_flip_byte(encrypted.wrap_nonce))
    elif tamper_target == "kek_key_id":
        tampered_encrypted = replace(encrypted, kek_key_id="test-kek-v2")
    elif tamper_target == "workspace_id":
        tampered_context = replace(context, workspace_id=uuid4())
    elif tamper_target == "connection_id":
        tampered_context = replace(context, connection_id=uuid4())
    elif tamper_target == "token_purpose":
        tampered_context = replace(context, token_purpose="other-token-purpose")
    elif tamper_target == "wrap_purpose":
        tampered_context = replace(context, wrap_purpose="other-wrap-purpose")
    else:
        raise AssertionError("unknown tamper target")

    with pytest.raises(CredentialVaultError) as raised:
        vault.decrypt(context=tampered_context, encrypted=tampered_encrypted)

    assert "synthetic-access-token" not in str(raised.value)
    assert "synthetic-refresh-token" not in repr(raised.value)


def test_key_rotation_decrypts_historical_rows_and_rewrap_preserves_token_ciphertext() -> None:
    v1_key_ring = CredentialKeyRing.from_keys(
        active_key_id="test-kek-v1",
        keys={"test-kek-v1": b"k" * 32},
    )
    context = CredentialContext.for_yandex_connection(
        workspace_id=uuid4(),
        connection_id=uuid4(),
    )
    payload = _credential_payload()
    encrypted = CredentialVault(v1_key_ring).encrypt(context=context, payload=payload)
    rotated_vault = CredentialVault(
        CredentialKeyRing.from_keys(
            active_key_id="test-kek-v2",
            keys={"test-kek-v1": b"k" * 32, "test-kek-v2": b"v" * 32},
        )
    )

    assert rotated_vault.decrypt(context=context, encrypted=encrypted) == payload
    rewrapped = rotated_vault.rewrap_to_active_key(context=context, encrypted=encrypted)

    assert rewrapped.kek_key_id == "test-kek-v2"
    assert rewrapped.token_ciphertext == encrypted.token_ciphertext
    assert rewrapped.token_nonce == encrypted.token_nonce
    assert rewrapped.wrapped_dek != encrypted.wrapped_dek
    assert rewrapped.wrap_nonce != encrypted.wrap_nonce
    assert rotated_vault.decrypt(context=context, encrypted=rewrapped) == payload
