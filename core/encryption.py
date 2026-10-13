"""Fernet symmetric encryption for API keys at rest."""
import os
import logging

log = logging.getLogger("bot.crypto")

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet

    key = os.environ.get("FERNET_KEY", "")
    if not key:
        log.warning("FERNET_KEY not set — encryption disabled, storing plaintext")
        return None

    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        return _fernet
    except Exception as e:
        log.error(f"Failed to initialize Fernet: {e}")
        return None


class EncryptionUnavailable(RuntimeError):
    """FERNET_KEY ausente/inválida al cifrar un secreto que no puede ir en claro."""


def encrypt_value(plaintext: str, required: bool = False) -> str:
    """Encrypt a string value. Returns ciphertext or plaintext if no key.

    required=True (API keys de trading, private keys de wallet): fail-closed —
    lanza EncryptionUnavailable en vez de guardar el secreto en claro.
    """
    if not plaintext:
        return ""
    f = _get_fernet()
    if f is None:
        if required:
            raise EncryptionUnavailable("FERNET_KEY no configurada: no se guardan secretos en claro")
        return plaintext
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a string value. Returns plaintext or the input if decryption fails."""
    if not ciphertext:
        return ""
    f = _get_fernet()
    if f is None:
        return ciphertext
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except Exception:
        return ciphertext
