from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from token_router.core.config import settings
from token_router.core.vault import hash_token
from token_router.db.session import get_db
from token_router.models.entities import Consumer, ConsumerApiKey


async def authenticate_consumer(
    x_consumer_key: str = Header(None, alias="X-Consumer-Key"),
    authorization: str = Header(None, alias="Authorization"),
    db: AsyncSession = Depends(get_db),
) -> Consumer:
    """Authenticates the consumer application via X-Consumer-Key or Bearer token."""
    raw_key = x_consumer_key
    if not raw_key and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw_key = parts[1]

    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Consumer API key is required. Supply via X-Consumer-Key header or Bearer token.",
        )

    key_hash = hash_token(raw_key)
    stmt = (
        select(ConsumerApiKey)
        .where(ConsumerApiKey.key_hash == key_hash, ConsumerApiKey.is_active.is_(True))
    )
    api_key_obj = (await db.execute(stmt)).scalar_one_or_none()

    if not api_key_obj:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, inactive, or revoked consumer API key.",
        )

    consumer = (await db.execute(select(Consumer).where(Consumer.id == api_key_obj.consumer_id))).scalar_one_or_none()
    if not consumer or not consumer.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Consumer account is disabled or inactive.",
        )

    return consumer


async def authenticate_admin(
    x_admin_key: str = Header(None, alias="X-Admin-Key"),
    authorization: str = Header(None, alias="Authorization"),
) -> bool:
    """Verifies master admin secret for platform and provider registration."""
    raw_key = x_admin_key
    if not raw_key and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw_key = parts[1]

    if not raw_key or raw_key != settings.ADMIN_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized admin access: Invalid X-Admin-Key.",
        )
    return True
