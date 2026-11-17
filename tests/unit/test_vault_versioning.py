"""Tests for Key Versioning, Key Rotation, and Envelope Decryption in Vault."""

import pytest
from token_router.core.vault import Vault


def test_vault_key_versioning_and_rotation():
    key_v1 = "initial_master_key_version_one_12345"
    key_v2 = "rotated_master_key_version_two_67890"

    v = Vault(key_material=key_v1, key_version=1)
    secret = "sk-super-secret-production-token"

    # Encrypt with Version 1
    c1 = v.encrypt(secret)
    assert v.decrypt(c1) == secret

    # Register Version 2 and set as primary
    v.register_key_version(version=2, key_material=key_v2)
    v.set_primary_version(2)

    # Encrypt with Version 2
    c2 = v.encrypt(secret)
    assert c2 != c1
    assert v.decrypt(c2) == secret

    # Old ciphertext encrypted with v1 can still be decrypted transparently!
    assert v.decrypt(c1) == secret

    # Re-encrypt old payload to target version 2
    c1_migrated = v.reencrypt(c1, target_version=2)
    assert v.decrypt(c1_migrated) == secret
