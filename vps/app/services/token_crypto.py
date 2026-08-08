"""AES-256-GCM envelope encryption for Automation Studio credentials."""
from __future__ import annotations

from base64 import b64decode, b64encode, urlsafe_b64decode, urlsafe_b64encode
from functools import lru_cache
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENVELOPE_VERSION = "v1"
KMS_CONTEXT_PURPOSE = "automation-studio-token-crypto"
AAD_PURPOSE = "automation-studio-token-v1"
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TokenEncryptionError(RuntimeError):
    """Base exception that never contains secret values."""


class TokenEncryptionConfigurationError(TokenEncryptionError):
    """Encryption key configuration is absent or invalid."""


class TokenDecryptionError(TokenEncryptionError):
    """Decryption failed without disclosing the exact reason."""


def _is_production() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() in {"prod", "production"}


def _strict_json_object(raw: str, env_name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise TokenEncryptionConfigurationError(f"{env_name.lower()}_invalid") from None
    if not isinstance(value, dict):
        raise TokenEncryptionConfigurationError(f"{env_name.lower()}_invalid")
    return value


def _decode_local_key(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        raise TokenEncryptionConfigurationError("token_local_keyring_invalid")
    try:
        raw = b64decode(value.encode("ascii"), validate=True)
    except Exception:
        raise TokenEncryptionConfigurationError("token_local_keyring_invalid") from None
    if len(raw) != 32 or b64encode(raw).decode("ascii") != value:
        raise TokenEncryptionConfigurationError("token_local_keyring_invalid")
    return raw


def _load_local_keyring(values: Mapping[str, Any]) -> dict[str, bytes]:
    keyring: dict[str, bytes] = {}
    for key_id, value in values.items():
        if not isinstance(key_id, str) or _KEY_ID_RE.fullmatch(key_id) is None:
            raise TokenEncryptionConfigurationError("token_local_keyring_invalid")
        keyring[key_id] = _decode_local_key(value)
    return keyring


def _read_local_keyring_file(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    try:
        file_stat = path.stat()
    except OSError:
        raise TokenEncryptionConfigurationError("token_keyring_file_invalid") from None
    if not stat.S_ISREG(file_stat.st_mode):
        raise TokenEncryptionConfigurationError("token_keyring_file_invalid")
    if _is_production() and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise TokenEncryptionConfigurationError("token_keyring_file_permissive")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise TokenEncryptionConfigurationError("token_keyring_file_invalid") from None
    return _strict_json_object(raw, "TOKEN_LOCAL_KEYRING_FILE")


def _load_kms_keyring(values: Mapping[str, Any]) -> dict[str, bytes]:
    region = os.getenv("TOKEN_KMS_REGION", "ap-northeast-1").strip()
    if not region:
        raise TokenEncryptionConfigurationError("token_kms_region_invalid")
    try:
        import boto3

        kms = boto3.client("kms", region_name=region)
    except Exception:
        raise TokenEncryptionConfigurationError("token_kms_client_unavailable") from None

    keyring: dict[str, bytes] = {}
    for key_id, encoded_blob in values.items():
        if not isinstance(key_id, str) or _KEY_ID_RE.fullmatch(key_id) is None:
            raise TokenEncryptionConfigurationError("token_kms_data_keys_invalid")
        if not isinstance(encoded_blob, str) or not encoded_blob:
            raise TokenEncryptionConfigurationError("token_kms_data_keys_invalid")
        try:
            ciphertext_blob = b64decode(encoded_blob.encode("ascii"), validate=True)
            response = kms.decrypt(
                CiphertextBlob=ciphertext_blob,
                EncryptionContext={"purpose": KMS_CONTEXT_PURPOSE, "key_id": key_id},
            )
            plaintext = bytes(response["Plaintext"])
        except Exception:
            raise TokenEncryptionConfigurationError("token_kms_decrypt_failed") from None
        if len(plaintext) != 32:
            raise TokenEncryptionConfigurationError("token_kms_plaintext_size_invalid")
        keyring[key_id] = plaintext
    return keyring


def _development_keyring() -> tuple[str, dict[str, bytes]]:
    active = "development"
    seed = os.getenv("TOKEN_DEVELOPMENT_KEY", "") or os.getenv(
        "SECRET_KEY", "dev-dummy-secret-please-change"
    )
    return active, {active: hashlib.sha256(("token-dev:" + seed).encode()).digest()}


@lru_cache(maxsize=1)
def load_keyring() -> tuple[str, Mapping[str, bytes]]:
    active = os.getenv("TOKEN_ACTIVE_KEY_ID", "").strip()
    kms_values = _strict_json_object(
        os.getenv("TOKEN_KMS_DATA_KEYS_JSON", "{}"),
        "TOKEN_KMS_DATA_KEYS_JSON",
    )
    if kms_values:
        if _KEY_ID_RE.fullmatch(active) is None or active not in kms_values:
            raise TokenEncryptionConfigurationError("token_active_key_invalid")
        return active, _load_kms_keyring(kms_values)

    local_file = os.getenv("TOKEN_LOCAL_KEYRING_FILE", "").strip()
    if local_file:
        local_values = _read_local_keyring_file(local_file)
        if not local_values:
            raise TokenEncryptionConfigurationError("token_local_keyring_file_invalid")
        if _KEY_ID_RE.fullmatch(active) is None or active not in local_values:
            raise TokenEncryptionConfigurationError("token_active_key_invalid")
        return active, _load_local_keyring(local_values)

    local_values = _strict_json_object(
        os.getenv("TOKEN_LOCAL_KEYRING_JSON", "{}"),
        "TOKEN_LOCAL_KEYRING_JSON",
    )
    if local_values:
        if _KEY_ID_RE.fullmatch(active) is None or active not in local_values:
            raise TokenEncryptionConfigurationError("token_active_key_invalid")
        return active, _load_local_keyring(local_values)
    if _is_production():
        raise TokenEncryptionConfigurationError("token_keyring_required")
    return _development_keyring()


def clear_keyring_cache() -> None:
    load_keyring.cache_clear()


def _b64url_encode(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    try:
        return urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))
    except Exception:
        raise TokenDecryptionError("token_envelope_invalid") from None


def _aad(field: str) -> bytes:
    if not isinstance(field, str) or not field or len(field) > 128:
        raise TokenEncryptionError("token_field_invalid")
    return f"{AAD_PURPOSE}|{field}".encode("utf-8")


def encrypt_json(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    active_key_id, keyring = load_keyring()
    plaintext = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    nonce = os.urandom(12)
    ciphertext = AESGCM(keyring[active_key_id]).encrypt(nonce, plaintext, _aad(field))
    return ":".join(
        (ENVELOPE_VERSION, active_key_id, _b64url_encode(nonce), _b64url_encode(ciphertext))
    )


def decrypt_json(token: str | None, *, field: str) -> Any:
    if token is None:
        return None
    try:
        version, key_id, nonce_text, ciphertext_text = token.split(":", 3)
    except (AttributeError, ValueError):
        raise TokenDecryptionError("token_envelope_invalid") from None
    if version != ENVELOPE_VERSION or _KEY_ID_RE.fullmatch(key_id) is None:
        raise TokenDecryptionError("token_envelope_invalid")
    _active, keyring = load_keyring()
    key = keyring.get(key_id)
    if key is None:
        raise TokenDecryptionError("token_key_unavailable")
    nonce = _b64url_decode(nonce_text)
    ciphertext = _b64url_decode(ciphertext_text)
    if len(nonce) != 12 or len(ciphertext) < 17:
        raise TokenDecryptionError("token_envelope_invalid")
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad(field))
        return json.loads(plaintext.decode("utf-8"))
    except Exception:
        raise TokenDecryptionError("token_decryption_failed") from None


def configuration_status() -> dict[str, Any]:
    try:
        _active, keyring = load_keyring()
    except TokenEncryptionConfigurationError as exc:
        return {"ready": False, "kms_managed": False, "key_count": 0, "error": str(exc)}
    kms_managed = bool(
        _strict_json_object(
            os.getenv("TOKEN_KMS_DATA_KEYS_JSON", "{}"),
            "TOKEN_KMS_DATA_KEYS_JSON",
        )
    )
    return {
        "ready": True,
        "kms_managed": kms_managed,
        "key_count": len(keyring),
        "error": None,
    }
