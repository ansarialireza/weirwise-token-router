"""Tests verifying strict multi-tenant isolation across consumers."""

import pytest
from token_router.models.entities import Consumer, Provider, ProviderCredential
from token_router.services.router_service import TokenRouterService


@pytest.mark.asyncio
async def test_consumer_scope_authorization_enforced(db_session):
    """Consumer restricted to 'chat' cannot invoke 'create_payment_intent'."""
    consumer_ai = Consumer(
        id="c-isolated-ai",
        name="weirwise_chatbot",
        is_active=True,
        allowed_capabilities=["chat"],
        allowed_providers=["openai"],
    )
    db_session.add(consumer_ai)
    await db_session.commit()

    with pytest.raises(PermissionError) as exc_info:
        await TokenRouterService.execute_route(
            db=db_session,
            consumer=consumer_ai,
            capability="create_payment_intent",
            model="default",
            request_payload={"amount": 1000},
        )

    assert "not authorized for capability: create_payment_intent" in str(exc_info.value)
