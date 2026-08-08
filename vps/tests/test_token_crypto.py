from __future__ import annotations

from base64 import b64encode
import json

import pytest

from app.services.token_crypto import (
    TokenEncryptionConfigurationError,
    clear_keyring_cache,
    decrypt_json,
    encrypt_json,
    load_keyring,
)


@pytest.fixture(autouse=True)
def reset_keyring_cache():
    clear_keyring_cache()
    yield
    clear_keyring_cache()


def configure_local_file(monkeypatch, path, *, environment="production"):
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.delenv("TOKEN_KMS_DATA_KEYS_JSON", raising=False)
    monkeypatch.setenv("TOKEN_LOCAL_KEYRING_FILE", str(path))
    monkeypatch.delenv("TOKEN_LOCAL_KEYRING_JSON", raising=False)
    monkeypatch.setenv("TOKEN_ACTIVE_KEY_ID", "k1")
    clear_keyring_cache()


def write_keyring(path, value: str, *, mode=0o600):
    path.write_text(json.dumps({"k1": value}), encoding="utf-8")
    path.chmod(mode)


def test_production_requires_explicit_keyring(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("TOKEN_KMS_DATA_KEYS_JSON", raising=False)
    monkeypatch.delenv("TOKEN_LOCAL_KEYRING_FILE", raising=False)
    monkeypatch.delenv("TOKEN_LOCAL_KEYRING_JSON", raising=False)
    clear_keyring_cache()

    with pytest.raises(TokenEncryptionConfigurationError) as error:
        load_keyring()

    assert str(error.value) == "token_keyring_required"


def test_local_keyring_file_round_trip(tmp_path, monkeypatch):
    keyring_path = tmp_path / "keyring.json"
    write_keyring(keyring_path, b64encode(b"k" * 32).decode("ascii"))
    configure_local_file(monkeypatch, keyring_path)

    encrypted = encrypt_json({"refresh_token": "secret"}, field="oauth_refresh_token")

    assert decrypt_json(encrypted, field="oauth_refresh_token") == {
        "refresh_token": "secret"
    }


def test_production_rejects_permissive_local_keyring_file(tmp_path, monkeypatch):
    keyring_path = tmp_path / "keyring.json"
    write_keyring(keyring_path, b64encode(b"k" * 32).decode("ascii"), mode=0o640)
    configure_local_file(monkeypatch, keyring_path)

    with pytest.raises(TokenEncryptionConfigurationError) as error:
        load_keyring()

    assert str(error.value) == "token_keyring_file_permissive"


def test_local_keyring_file_rejects_non_32_byte_key(tmp_path, monkeypatch):
    keyring_path = tmp_path / "keyring.json"
    write_keyring(keyring_path, b64encode(b"short-key").decode("ascii"))
    configure_local_file(monkeypatch, keyring_path)

    with pytest.raises(TokenEncryptionConfigurationError):
        load_keyring()


def test_local_keyring_errors_do_not_expose_file_contents(tmp_path, monkeypatch):
    keyring_path = tmp_path / "keyring.json"
    secret_value = "sensitive-key-material-not-base64"
    raw_json = json.dumps({"k1": secret_value})
    keyring_path.write_text(raw_json, encoding="utf-8")
    keyring_path.chmod(0o600)
    configure_local_file(monkeypatch, keyring_path)

    with pytest.raises(TokenEncryptionConfigurationError) as error:
        load_keyring()

    message = str(error.value)
    assert secret_value not in message
    assert raw_json not in message
