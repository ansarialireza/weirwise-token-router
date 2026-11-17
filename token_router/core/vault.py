import base64
import hashlib
import json
import os
import secrets
from typing import Any, Dict, Optional, Tuple
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
    """AES-256 GCM authenticated encryption engine with Key Versioning support."""

    def __init__(self, key_material: Optional[str] = None, key_version: int = 1):
        raw_key = key_material or settings.VAULT_MASTER_KEY or "wtr_dev_fallback_key_32_bytes_long_entropy!"
        self._primary_version = key_version
        self._keys: Dict[int, AESGCM] = {
            self._primary_version: AESGCM(_derive_vault_key(raw_key))
        }

    def register_key_version(self, version: int, key_material: str) -> None:
        """Register an older or newer key version for zero-downtime rotation."""
        self._keys[version] = AESGCM(_derive_vault_key(key_material))

    def set_primary_version(self, version: int) -> None:
        if version not in self._keys:
            raise ValueError(f"Key version {version} not registered in vault.")
        self._primary_version = version

    def encrypt(self, plaintext: str, key_version: Optional[int] = None) -> str:
        """Encrypts plaintext and packages version, nonce, and ciphertext into a JSON envelope."""
        v = key_version or self._primary_version
        if v not in self._keys:
            raise ValueError(f"Vault encryption key version {v} not found.")

        aesgcm = self._keys[v]
        nonce = os.urandom(12)  # 96-bit standard nonce for GCM
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)

        envelope = {
            "v": v,
            "n": base64.urlsafe_b64encode(nonce).decode("utf-8"),
            "c": base64.urlsafe_b64encode(ciphertext).decode("utf-8"),
        }
        return base64.urlsafe_b64encode(json.dumps(envelope).encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_payload: str) -> str:
        """Decrypts versioned JSON envelope or legacy raw format."""
        raw_bytes = base64.urlsafe_b64decode(encrypted_payload.encode("utf-8"))
        try:
            # Check if modern versioned JSON envelope
            envelope = json.loads(raw_bytes.decode("utf-8"))
            if isinstance(envelope, dict) and "v" in envelope and "n" in envelope and "c" in envelope:
                version = envelope["v"]
                if version not in self._keys:
                    raise ValueError(f"Key version {version} required for decryption is not registered.")
                nonce = base64.urlsafe_b64decode(envelope["n"].encode("utf-8"))
                ciphertext = base64.urlsafe_b64decode(envelope["c"].encode("utf-8"))
                decrypted = self._keys[version].decrypt(nonce, ciphertext, None)
                return decrypted.decode("utf-8")
        except Exception:
            pass

        # Legacy fallback: raw 12-byte nonce followed by ciphertext
        nonce = raw_bytes[:12]
        ciphertext = raw_bytes[12:]
        decrypted = self._keys[self._primary_version].decrypt(nonce, ciphertext, None)
        return decrypted.decode("utf-8")

    def reencrypt(self, encrypted_payload: str, target_version: Optional[int] = None) -> str:
        """Decrypts with corresponding version and re-encrypts with latest target version."""
        plaintext = self.decrypt(encrypted_payload)
        return self.encrypt(plaintext, key_version=target_version or self._primary_version)


vault = Vault()


def hash_token(token: str) -> str:
    """Compute SHA-256 fingerprint for API keys or credentials."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_api_key(prefix: str = "wtr_live_") -> Tuple[str, str, str]:
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
