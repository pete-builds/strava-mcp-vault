"""Tests for cache/encryption.py."""

from cryptography.fernet import Fernet

import cache.encryption as enc


def _reset_encryption():
    """Reset module-level state so each test starts clean."""
    enc._fernet = None
    enc._initialized = False


def test_encrypt_decrypt_roundtrip(monkeypatch):
    _reset_encryption()
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", key)

    ciphertext = enc.encrypt_token("my-secret-token")
    assert ciphertext != "my-secret-token"
    assert enc.decrypt_token(ciphertext) == "my-secret-token"


def test_no_key_passthrough(monkeypatch):
    _reset_encryption()
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)

    assert enc.encrypt_token("plain") == "plain"
    assert enc.decrypt_token("plain") == "plain"


def test_decrypt_pre_encryption_data(monkeypatch):
    """If data was stored before encryption was enabled, decrypt returns as-is."""
    _reset_encryption()
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", key)

    result = enc.decrypt_token("not-a-fernet-token")
    assert result == "not-a-fernet-token"


def test_invalid_key_fallback(monkeypatch):
    """Invalid key should fall back to plaintext mode."""
    _reset_encryption()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "not-a-valid-key")

    assert enc.encrypt_token("plain") == "plain"
    assert enc.decrypt_token("plain") == "plain"
