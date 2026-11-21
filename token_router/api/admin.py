import uuid
from datetime import UTC, datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from token_router.adapters.registry import get_adapter, list_supported_providers
from token_router.api.dependencies import authenticate_admin
from token_router.core.vault import generate_api_key, hash_token, mask_credential, vault
from token_router.db.session import get_db
from token_router.models.entities import Consumer, ConsumerApiKey, Provider, ProviderCredential, UsageRecord
from token_router.schemas.contracts import (
    ConsumerCreate,
    ConsumerKeyCreate,
    ConsumerKeyResponse,
    ConsumerResponse,
    CredentialCreate,
    CredentialResponse,
    ProviderCreate,
    ProviderResponse,
)

admin_router = APIRouter(prefix="/api/v1/admin", tags=["Admin Operations"], dependencies=[Depends(authenticate_admin)])


# --- Consumers Management ---
@admin_router.post("/consumers", response_model=ConsumerResponse, status_code=status.HTTP_201_CREATED)
async def create_consumer(payload: ConsumerCreate, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(select(Consumer).where(Consumer.name == payload.name))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Consumer with this name already exists.")

    consumer = Consumer(
        name=payload.name,
        description=payload.description,
        allowed_capabilities=payload.allowed_capabilities,
        allowed_providers=payload.allowed_providers,
        monthly_budget_usd=payload.monthly_budget_usd,
    )
    db.add(consumer)
    await db.commit()
    await db.refresh(consumer)
    return consumer


@admin_router.get("/consumers", response_model=List[ConsumerResponse])
async def list_consumers(db: AsyncSession = Depends(get_db)):
    return list((await db.execute(select(Consumer))).scalars().all())


@admin_router.post("/consumers/{consumer_id}/keys", response_model=ConsumerKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_consumer_api_key(consumer_id: str, payload: ConsumerKeyCreate, db: AsyncSession = Depends(get_db)):
    consumer = (await db.execute(select(Consumer).where(Consumer.id == consumer_id))).scalar_one_or_none()
    if not consumer:
        raise HTTPException(status_code=404, detail="Consumer not found.")

    full_key, key_hash, key_prefix = generate_api_key()
    api_key = ConsumerApiKey(
        consumer_id=consumer.id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        label=payload.label,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return ConsumerKeyResponse(
        id=api_key.id,
        consumer_id=api_key.consumer_id,
        label=api_key.label,
        key_prefix=api_key.key_prefix,
        api_key=full_key,  # Returned only once!
        created_at=api_key.created_at,
    )


# --- Providers Management ---
@admin_router.post("/providers", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
async def register_provider(payload: ProviderCreate, db: AsyncSession = Depends(get_db)):
    if payload.name.lower() not in list_supported_providers():
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider: {payload.name}. Supported: {list_supported_providers()}",
        )

    existing = (await db.execute(select(Provider).where(Provider.name == payload.name.lower()))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Provider already registered.")

    provider = Provider(
        name=payload.name.lower(),
        display_name=payload.display_name,
        description=payload.description,
        capabilities=payload.capabilities,
        supported_models=payload.supported_models,
        default_routing_strategy=payload.default_routing_strategy,
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return provider


@admin_router.get("/providers", response_model=List[ProviderResponse])
async def list_providers(db: AsyncSession = Depends(get_db)):
    return list((await db.execute(select(Provider))).scalars().all())


# --- Credentials Management ---
@admin_router.post("/providers/{provider_id}/credentials", response_model=CredentialResponse, status_code=status.HTTP_201_CREATED)
async def register_credential(provider_id: str, payload: CredentialCreate, db: AsyncSession = Depends(get_db)):
    provider = (await db.execute(select(Provider).where(Provider.id == provider_id))).scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found.")

    secret_hash = hash_token(payload.secret)
    existing = (await db.execute(select(ProviderCredential).where(ProviderCredential.secret_hash == secret_hash))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="This credential secret has already been registered.")

    encrypted_secret = vault.encrypt(payload.secret)
    key_prefix = payload.secret[:8] if len(payload.secret) >= 8 else "wtr_cred"

    credential = ProviderCredential(
        provider_id=provider.id,
        label=payload.label,
        key_prefix=key_prefix,
        encrypted_secret=encrypted_secret,
        secret_hash=secret_hash,
        health_status="unknown",  # Initial state is unknown until tested
        priority=payload.priority,
        routing_weight=payload.routing_weight,
        models_allowed=payload.models_allowed,
        monthly_budget_usd=payload.monthly_budget_usd,
        rate_limit_rpm=payload.rate_limit_rpm,
        rate_limit_tpm=payload.rate_limit_tpm,
        max_concurrency=payload.max_concurrency,
    )
    db.add(credential)
    await db.commit()
    await db.refresh(credential)
    return credential


@admin_router.post("/credentials/{credential_id}/validate")
async def trigger_credential_validation(credential_id: str, db: AsyncSession = Depends(get_db)):
    """Triggers background or on-demand credential validation against live provider auth endpoint."""
    cred = (await db.execute(select(ProviderCredential).where(ProviderCredential.id == credential_id))).scalar_one_or_none()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found.")

    provider = (await db.execute(select(Provider).where(Provider.id == cred.provider_id))).scalar_one()
    adapter = get_adapter(provider.name)
    secret = vault.decrypt(cred.encrypted_secret)

    is_valid, message, status_code = await adapter.validate_credential(secret)
    cred.last_health_check_at = datetime.now(UTC)
    cred.health_status = "healthy" if is_valid else "failed"
    cred.last_error_reason = None if is_valid else message
    cred.last_error_code = status_code
    await db.commit()

    return {
        "credential_id": cred.id,
        "is_valid": is_valid,
        "health_status": cred.health_status,
        "message": message,
        "status_code": status_code,
    }


@admin_router.post("/emergency-stop")
async def emergency_stop_provider(provider_id: str, db: AsyncSession = Depends(get_db)):
    """Emergency shutdown of an entire provider or credential set."""
    provider = (await db.execute(select(Provider).where(Provider.id == provider_id))).scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found.")

    provider.is_enabled = False
    await db.commit()
    return {"status": "disabled", "provider": provider.name}


# --- Pricing & Quota Management ---
from token_router.core.pricing import ModelPricing, PricingRegistry
from token_router.core.quota import QuotaManager


@admin_router.get("/pricing", response_model=List[ModelPricing])
async def list_admin_pricing(provider: Optional[str] = None):
    return PricingRegistry.list_pricing(provider=provider)


@admin_router.post("/pricing", response_model=ModelPricing, status_code=status.HTTP_201_CREATED)
async def update_or_create_pricing(payload: ModelPricing):
    PricingRegistry.register_model_pricing(payload)
    return payload


@admin_router.get("/credentials/{credential_id}/quotas")
async def get_admin_credential_quota(credential_id: str):
    return await QuotaManager.get_credential_metrics(credential_id)
