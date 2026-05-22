"""Optional Fernet encryption for tokens at rest.

If TOKEN_ENCRYPTION_KEY is set in the environment, tokens are encrypted
before writing to SQLite and decrypted on read. If not set, tokens are
stored and returned as plaintext (backwards compatible).
"""

import logging
import os

logger = logging.getLogger(__name__)

_fernet = None
_initialized = False


def _init():
    global _fernet, _initialized
    if _initialized:
        return
    _initialized = True

    key = os.getenv("TOKEN_ENCRYPTION_KEY")
    if not key:
        logger.info("TOKEN_ENCRYPTION_KEY not set; tokens stored in plaintext")
        return

    try:
        from cryptography.fernet import Fernet

        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        logger.info("Token encryption enabled")
    except Exception as e:
        logger.error("Invalid TOKEN_ENCRYPTION_KEY, falling back to plaintext: %s", e)
        _fernet = None


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string. Returns plaintext if encryption is not configured."""
    _init()
    if _fernet is None:
        return plaintext
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(stored: str) -> str:
    """Decrypt a token string. Returns the input unchanged if encryption is not configured.

    Raises cryptography.fernet.InvalidToken if the stored value looks like
    Fernet ciphertext but cannot be decrypted (e.g. TOKEN_ENCRYPTION_KEY was
    rotated). Values that do not look like Fernet ciphertext are assumed to
    be pre-encryption plaintext and returned as-is — supports the one-time
    migration when encryption is enabled on an existing deployment.
    """
    _init()
    if _fernet is None:
        return stored

    # Fernet ciphertext is URL-safe base64 starting with the version byte 0x80,
    # which encodes to "gAAAAA". If the stored value doesn't look like Fernet,
    # it predates encryption being turned on.
    if not stored.startswith("gAAAAA"):
        logger.info("Token stored as plaintext; will be re-encrypted on next refresh")
        return stored

    from cryptography.fernet import InvalidToken

    try:
        return _fernet.decrypt(stored.encode()).decode()
    except InvalidToken:
        logger.error(
            "Token decryption failed. TOKEN_ENCRYPTION_KEY may have been rotated. "
            "Re-seed tokens via STRAVA_ACCESS_TOKEN / STRAVA_REFRESH_TOKEN env vars."
        )
        raise
