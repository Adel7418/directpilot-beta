from __future__ import annotations

import base64
import json
import os
import re
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CREDENTIAL_SCHEMA_VERSION = 1
YANDEX_PROVIDER = "yandex"
_TOKEN_PAYLOAD_PURPOSE = "yandex-token-payload"
_DEK_WRAP_PURPOSE = "yandex-dek-wrap"
_NONCE_LENGTH = 12
_DEK_LENGTH = 32
_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_BASE64URL_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}=?\Z")


class CredentialVaultError(RuntimeError):
    """Base class for safe credential-vault failures."""


class CredentialConfigurationError(CredentialVaultError):
    """Raised for an invalid external credential-key configuration."""


class CredentialKeyUnavailable(CredentialVaultError):
    """Raised when a configured or historical KEK cannot be used."""


class CredentialEncryptionError(CredentialVaultError):
    """Raised when credentials cannot be encrypted safely."""


class CredentialDecryptionError(CredentialVaultError):
    """Raised when an encrypted credential cannot be authenticated or decoded."""


class CredentialPayloadError(CredentialVaultError):
    """Raised when a credential payload cannot satisfy the bounded schema."""


@dataclass(frozen=True, slots=True)
class CredentialContext:
    """Non-secret identifiers bound into both AES-GCM authenticated-data layers."""

    workspace_id: UUID
    connection_id: UUID
    schema_version: int = CREDENTIAL_SCHEMA_VERSION
    provider: str = YANDEX_PROVIDER
    token_purpose: str = _TOKEN_PAYLOAD_PURPOSE
    wrap_purpose: str = _DEK_WRAP_PURPOSE

    @classmethod
    def for_yandex_connection(
        cls,
        *,
        workspace_id: UUID,
        connection_id: UUID,
    ) -> CredentialContext:
        return cls(workspace_id=workspace_id, connection_id=connection_id)

    def __post_init__(self) -> None:
        if self.schema_version <= 0:
            raise CredentialConfigurationError("Credential context is invalid")
        if not self.provider or not self.provider.isascii() or len(self.provider) > 32:
            raise CredentialConfigurationError("Credential context is invalid")
        for purpose in (self.token_purpose, self.wrap_purpose):
            if not purpose or not purpose.isascii() or len(purpose) > 128:
                raise CredentialConfigurationError("Credential context is invalid")


@dataclass(frozen=True, slots=True)
class YandexCredentialPayload:
    """Opaque plaintext token value held only in the bounded provider-call scope."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_token_value(self.access_token)
        _validate_token_value(self.refresh_token)
        object.__setattr__(
            self,
            "access_token_expires_at",
            _normalized_timestamp(self.access_token_expires_at),
        )
        if self.refresh_token_expires_at is not None:
            object.__setattr__(
                self,
                "refresh_token_expires_at",
                _normalized_timestamp(self.refresh_token_expires_at),
            )


@dataclass(frozen=True, slots=True)
class EncryptedYandexCredential:
    """Persistable envelope fields; repr intentionally excludes encrypted material too."""

    token_ciphertext: bytes = field(repr=False)
    token_nonce: bytes = field(repr=False)
    wrapped_dek: bytes = field(repr=False)
    wrap_nonce: bytes = field(repr=False)
    kek_key_id: str
    schema_version: int
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.schema_version <= 0 or not _is_valid_key_id(self.kek_key_id):
            raise CredentialDecryptionError("Encrypted credential metadata is invalid")
        if not isinstance(self.token_ciphertext, bytes) or not self.token_ciphertext:
            raise CredentialDecryptionError("Encrypted credential metadata is invalid")
        if not isinstance(self.wrapped_dek, bytes) or not self.wrapped_dek:
            raise CredentialDecryptionError("Encrypted credential metadata is invalid")
        if not isinstance(self.token_nonce, bytes) or len(self.token_nonce) != _NONCE_LENGTH:
            raise CredentialDecryptionError("Encrypted credential metadata is invalid")
        if not isinstance(self.wrap_nonce, bytes) or len(self.wrap_nonce) != _NONCE_LENGTH:
            raise CredentialDecryptionError("Encrypted credential metadata is invalid")
        object.__setattr__(
            self,
            "access_token_expires_at",
            _normalized_timestamp(self.access_token_expires_at),
        )
        if self.refresh_token_expires_at is not None:
            object.__setattr__(
                self,
                "refresh_token_expires_at",
                _normalized_timestamp(self.refresh_token_expires_at),
            )


@dataclass(frozen=True, slots=True)
class CredentialKeyRing:
    """Versioned KEKs injected from a secret mount, never persisted or repr'd."""

    active_key_id: str
    keys: Mapping[str, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        if not _is_valid_key_id(self.active_key_id):
            raise CredentialConfigurationError("Credential key ring is invalid")
        normalized: dict[str, bytes] = {}
        for key_id, key_material in self.keys.items():
            if not _is_valid_key_id(key_id) or not isinstance(key_material, bytes):
                raise CredentialConfigurationError("Credential key ring is invalid")
            if len(key_material) != _DEK_LENGTH:
                raise CredentialConfigurationError("Credential key ring is invalid")
            normalized[key_id] = bytes(key_material)
        if not normalized or self.active_key_id not in normalized:
            raise CredentialConfigurationError("Credential key ring is invalid")
        object.__setattr__(self, "keys", MappingProxyType(normalized))

    @classmethod
    def from_keys(
        cls,
        *,
        active_key_id: str,
        keys: Mapping[str, bytes],
    ) -> CredentialKeyRing:
        """Construct an injectable in-memory key ring for tests and bootstrapping."""
        return cls(active_key_id=active_key_id, keys=keys)

    @classmethod
    def from_json_secret_file(cls, path: str | Path) -> CredentialKeyRing:
        """Load base64url 256-bit KEKs from a mode-restricted JSON secret file."""
        secret_path = Path(path)
        try:
            file_status = secret_path.stat()
            if not stat.S_ISREG(file_status.st_mode):
                raise CredentialConfigurationError("Credential key-ring secret file is invalid")
            if os.name == "posix" and stat.S_IMODE(file_status.st_mode) & 0o077:
                raise CredentialConfigurationError("Credential key-ring secret file permissions are unsafe")
            serialized = secret_path.read_text(encoding="utf-8")
        except CredentialConfigurationError:
            raise
        except (OSError, UnicodeDecodeError):
            raise CredentialConfigurationError("Credential key-ring secret file is unavailable") from None
        try:
            document = json.loads(serialized, object_pairs_hook=_json_object_without_duplicates)
        except (json.JSONDecodeError, _DuplicateJsonKey):
            raise CredentialConfigurationError("Credential key-ring secret file is invalid") from None
        if not isinstance(document, dict) or set(document) != {"active_key_id", "keys"}:
            raise CredentialConfigurationError("Credential key-ring secret file is invalid")
        active_key_id = document["active_key_id"]
        encoded_keys = document["keys"]
        if not isinstance(active_key_id, str) or not isinstance(encoded_keys, dict):
            raise CredentialConfigurationError("Credential key-ring secret file is invalid")
        decoded_keys: dict[str, bytes] = {}
        for key_id, encoded_key in encoded_keys.items():
            if not isinstance(key_id, str) or not isinstance(encoded_key, str):
                raise CredentialConfigurationError("Credential key-ring secret file is invalid")
            decoded_keys[key_id] = _decode_base64url_key(encoded_key)
        return cls.from_keys(active_key_id=active_key_id, keys=decoded_keys)

    def active_key(self) -> tuple[str, bytes]:
        return self.active_key_id, self.keys[self.active_key_id]

    def key_for(self, key_id: str) -> bytes:
        try:
            return self.keys[key_id]
        except KeyError:
            raise CredentialKeyUnavailable("Credential key is unavailable") from None


class CredentialVault:
    """AES-256-GCM envelope encryption for one Yandex connection credential payload."""

    def __init__(self, key_ring: CredentialKeyRing) -> None:
        self._key_ring = key_ring

    def encrypt(
        self,
        *,
        context: CredentialContext,
        payload: YandexCredentialPayload,
    ) -> EncryptedYandexCredential:
        if context.schema_version != CREDENTIAL_SCHEMA_VERSION or context.provider != YANDEX_PROVIDER:
            raise CredentialEncryptionError("Credential encryption context is invalid")
        plaintext = _serialize_payload(payload)
        dek = os.urandom(_DEK_LENGTH)
        try:
            token_nonce = os.urandom(_NONCE_LENGTH)
            wrap_nonce = os.urandom(_NONCE_LENGTH)
            key_id, kek = self._key_ring.active_key()
            token_ciphertext = AESGCM(dek).encrypt(
                token_nonce,
                plaintext,
                _authenticated_data(context, context.token_purpose),
            )
            wrapped_dek = AESGCM(kek).encrypt(
                wrap_nonce,
                dek,
                _authenticated_data(context, context.wrap_purpose, kek_key_id=key_id),
            )
            return EncryptedYandexCredential(
                token_ciphertext=token_ciphertext,
                token_nonce=token_nonce,
                wrapped_dek=wrapped_dek,
                wrap_nonce=wrap_nonce,
                kek_key_id=key_id,
                schema_version=context.schema_version,
                access_token_expires_at=payload.access_token_expires_at,
                refresh_token_expires_at=payload.refresh_token_expires_at,
            )
        except (CredentialVaultError, ValueError):
            raise
        except Exception:
            raise CredentialEncryptionError("Credential encryption failed") from None
        finally:
            del plaintext
            del dek

    def decrypt(
        self,
        *,
        context: CredentialContext,
        encrypted: EncryptedYandexCredential,
    ) -> YandexCredentialPayload:
        dek = self._unwrap_dek(context=context, encrypted=encrypted)
        try:
            plaintext = AESGCM(dek).decrypt(
                encrypted.token_nonce,
                encrypted.token_ciphertext,
                _authenticated_data(context, context.token_purpose),
            )
            payload = _deserialize_payload(plaintext)
            if (
                payload.access_token_expires_at != encrypted.access_token_expires_at
                or payload.refresh_token_expires_at != encrypted.refresh_token_expires_at
            ):
                raise CredentialDecryptionError("Encrypted credential metadata is invalid")
            return payload
        except CredentialVaultError:
            raise
        except (InvalidTag, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise CredentialDecryptionError("Credential decryption failed") from None
        finally:
            del dek

    def rewrap_to_active_key(
        self,
        *,
        context: CredentialContext,
        encrypted: EncryptedYandexCredential,
    ) -> EncryptedYandexCredential:
        """Rotate only the wrapped DEK layer; token ciphertext and nonce stay byte-identical."""
        dek = self._unwrap_dek(context=context, encrypted=encrypted)
        try:
            active_key_id, active_kek = self._key_ring.active_key()
            wrap_nonce = os.urandom(_NONCE_LENGTH)
            wrapped_dek = AESGCM(active_kek).encrypt(
                wrap_nonce,
                dek,
                _authenticated_data(context, context.wrap_purpose, kek_key_id=active_key_id),
            )
            return replace(
                encrypted,
                wrapped_dek=wrapped_dek,
                wrap_nonce=wrap_nonce,
                kek_key_id=active_key_id,
            )
        except CredentialVaultError:
            raise
        except Exception:
            raise CredentialEncryptionError("Credential rewrap failed") from None
        finally:
            del dek

    def _unwrap_dek(
        self,
        *,
        context: CredentialContext,
        encrypted: EncryptedYandexCredential,
    ) -> bytes:
        if (
            context.schema_version != CREDENTIAL_SCHEMA_VERSION
            or context.provider != YANDEX_PROVIDER
            or encrypted.schema_version != context.schema_version
        ):
            raise CredentialDecryptionError("Encrypted credential metadata is invalid")
        kek = self._key_ring.key_for(encrypted.kek_key_id)
        try:
            dek = AESGCM(kek).decrypt(
                encrypted.wrap_nonce,
                encrypted.wrapped_dek,
                _authenticated_data(
                    context,
                    context.wrap_purpose,
                    kek_key_id=encrypted.kek_key_id,
                ),
            )
        except (InvalidTag, ValueError):
            raise CredentialDecryptionError("Credential decryption failed") from None
        if len(dek) != _DEK_LENGTH:
            raise CredentialDecryptionError("Credential decryption failed")
        return dek


class _DuplicateJsonKey(ValueError):
    pass


def _json_object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise _DuplicateJsonKey
        document[key] = value
    return document


def _decode_base64url_key(value: str) -> bytes:
    if not _BASE64URL_KEY_PATTERN.fullmatch(value):
        raise CredentialConfigurationError("Credential key-ring secret file is invalid")
    try:
        decoded = base64.b64decode(
            value + ("=" * (-len(value) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except ValueError:
        raise CredentialConfigurationError("Credential key-ring secret file is invalid") from None
    if len(decoded) != _DEK_LENGTH:
        raise CredentialConfigurationError("Credential key-ring secret file is invalid")
    return decoded


def _is_valid_key_id(value: str) -> bool:
    return bool(_KEY_ID_PATTERN.fullmatch(value))


def _validate_token_value(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 16_384:
        raise CredentialPayloadError("Credential payload is invalid")


def _normalized_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CredentialPayloadError("Credential payload is invalid")
    return value.astimezone(timezone.utc)


def _datetime_to_payload_value(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime_from_payload_value(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CredentialDecryptionError("Credential payload is invalid")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        raise CredentialDecryptionError("Credential payload is invalid") from None
    return _normalized_timestamp(parsed)


def _serialize_payload(payload: YandexCredentialPayload) -> bytes:
    document = {
        "access_token": payload.access_token,
        "access_token_expires_at": _datetime_to_payload_value(payload.access_token_expires_at),
        "refresh_token": payload.refresh_token,
        "refresh_token_expires_at": _datetime_to_payload_value(payload.refresh_token_expires_at),
        "schema_version": CREDENTIAL_SCHEMA_VERSION,
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _deserialize_payload(serialized: bytes) -> YandexCredentialPayload:
    try:
        document = json.loads(serialized.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CredentialDecryptionError("Credential payload is invalid") from None
    if not isinstance(document, dict) or set(document) != {
        "access_token",
        "access_token_expires_at",
        "refresh_token",
        "refresh_token_expires_at",
        "schema_version",
    }:
        raise CredentialDecryptionError("Credential payload is invalid")
    if document["schema_version"] != CREDENTIAL_SCHEMA_VERSION:
        raise CredentialDecryptionError("Credential payload is invalid")
    try:
        return YandexCredentialPayload(
            access_token=document["access_token"],
            refresh_token=document["refresh_token"],
            access_token_expires_at=_required_timestamp(document["access_token_expires_at"]),
            refresh_token_expires_at=_datetime_from_payload_value(
                document["refresh_token_expires_at"]
            ),
        )
    except CredentialPayloadError:
        raise CredentialDecryptionError("Credential payload is invalid") from None


def _required_timestamp(value: object) -> datetime:
    parsed = _datetime_from_payload_value(value)
    if parsed is None:
        raise CredentialDecryptionError("Credential payload is invalid")
    return parsed


def _authenticated_data(
    context: CredentialContext,
    purpose: str,
    *,
    kek_key_id: str | None = None,
) -> bytes:
    document: dict[str, int | str] = {
        "connection_id": str(context.connection_id),
        "provider": context.provider,
        "purpose": purpose,
        "schema_version": context.schema_version,
        "workspace_id": str(context.workspace_id),
    }
    if kek_key_id is not None:
        document["kek_key_id"] = kek_key_id
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("ascii")
