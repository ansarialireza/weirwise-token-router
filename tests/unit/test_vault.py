"""Comprehensive unit test suite for Token Router core components."""

import pytest
from token_router.core.vault import Vault, hash_token, generate_api_key, mask_credential


def test_vault_encryption_decryption():
    v = Vault("super_secret_test_master_key_123456789")
    secret = "sk-proj-1234567890abcdefghijklmnopqrstuvwxyz"
    encrypted = v.encrypt(secret)
    assert encrypted != secret
    assert len(encrypted) > len(secret)
    decrypted = v.decrypt(encrypted)
    assert decrypted == secret


def test_vault_unique_ciphertexts_for_same_plaintext():
    v = Vault()
    secret = "same_secret_payload"
    c1 = v.encrypt(secret)
    c2 = v.encrypt(secret)
    assert c1 != c2  # Unique random nonces
    assert v.decrypt(c1) == secret
    assert v.decrypt(c2) == secret


def test_hash_token_deterministic():
    t = "wtr_live_test_api_key_12345"
    h1 = hash_token(t)
    h2 = hash_token(t)
    assert h1 == h2
    assert len(h1) == 64


def test_generate_api_key_format():
    full_key, key_hash, key_prefix = generate_api_key(prefix="wtr_live_")
    assert full_key.startswith("wtr_live_")
    assert len(full_key) > 30
    assert hash_token(full_key) == key_hash
    assert full_key.startswith(key_prefix)


def test_mask_credential():
    secret = "sk-ant-api03-abcdefghijklmnop"
    masked = mask_credential(secret, prefix_len=6, suffix_len=4)
    assert masked == "sk-ant...mnop"
    assert "abcdef" not in masked
