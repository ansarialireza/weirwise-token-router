import base64
import hashlib
import os
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from token_router.core.config import settings


def _derive_vault_key(secret_or_key: str) -> bytes:
    """Derive deterministic 32-byte key from configuration string."""
    try:
        decoded = base64.urlsafe_b64decode(secret_or_key.encode("utf-8"))
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass
    # Fallback to SHA-256 derivation
    return hashlib.sha256(secret_or_key.encode("utf-8")).digest()


class Vault:
    """AES-256 GCM authenticated encryption engine for provider secrets."""

    def __init__(self, key_material: str | None = None):
        self._key = _derive_vault_key(key_material or settings.VAULT_MASTER_KEY)
        self._aesgcm = AESGCM(self._key)

    def encrypt(self, plaintext: str) -> str:
        """Encrypts plaintext string and returns URL-safe base64 string containing nonce + ciphertext."""
        nonce = os.urandom(12)  # 96-bit nonce for AES-GCM
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("utf-8")

    def decrypt(self, encrypted_payload: str) -> str:
        """Decrypts URL-safe base64 string containing nonce + ciphertext back to plaintext."""
        raw = base64.urlsafe_b64decode(encrypted_payload.encode("utf-8"))
        nonce = raw[:12]
        ciphertext = raw[12:]
        decrypted = self._aesgcm.decrypt(nonce, ciphertext, None)
        return decrypted.decode("utf-8")


vault = Vault()


def hash_token(token: str) -> str:
    """Compute SHA-256 fingerprint for API keys or credentials."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_api_key(prefix: str = "wtr_live_") -> tuple[str, str, str]:
    """Generate a high-entropy consumer API key. Returns (full_key, key_hash, key_prefix)."""
    random_bytes = secrets.token_hex(24)
    full_key = f"{prefix}{random_bytes}"
    key_hash = hash_token(full_key)
    key_prefix = full_key[:12]
    return full_key, key_hash, key_prefix


def mask_credential(secret: str, prefix_len: int = 4, suffix_len: int = 4) -> str:
    """Safely mask credential, e.g. 'sk-1234567890abcdef' -> 'sk-1...cdef'."""
    if len(secret) <= (prefix_len + suffix_len):
        return "****"
    return f"{secret[:prefix_len]}...{secret[-suffix_len:]}"
