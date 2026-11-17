"""Security Verification: Insecure Defaults Rejection, API Scopes, and Secret Redaction."""

import pytest
from token_router.core.config import Settings
from token_router.core.vault import mask_credential


def test_production_fails_if_secrets_missing():
    # Production with default/missing master key must raise RuntimeError
    insecure_prod = Settings(
        ENVIRONMENT="production",
        VAULT_MASTER_KEY=None,
        ADMIN_API_KEY=None,
        DATABASE_URL="sqlite+aiosqlite:///./test.db",
    )
    with pytest.raises(RuntimeError) as exc_info:
        insecure_prod.validate_production_secrets()
    err = str(exc_info.value)
    assert "VAULT_MASTER_KEY" in err
    assert "ADMIN_API_KEY" in err
    assert "DATABASE_URL" in err


def test_secret_masking_never_reveals_raw():
    secret = "sk-live-thisisalongsecretkeyforproduction123456789"
    masked = mask_credential(secret)
    assert secret not in masked
    assert "****" in masked or "..." in masked
