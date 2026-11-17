"""Tests verifying that sensitive operations (requires_approval=True) CANNOT be executed without valid approval."""

import pytest
from unittest.mock import AsyncMock, patch
from token_router.core.vault import vault
from token_router.models.entities import Consumer, Provider, ProviderCredential
from token_router.services.approvals import ApprovalManager
from token_router.services.router_service import TokenRouterService


@pytest.mark.asyncio
async def test_refund_payment_blocked_without_approval(db_session):
    """Calling refund_payment without approval_id raises PermissionError."""
    consumer = Consumer(
        id="c-refund-test",
        name="test_refund_client",
        is_active=True,
        allowed_capabilities=["refund_payment"],
        allowed_providers=["*"],
    )
    db_session.add(consumer)

    p_stripe = Provider(
        id="p-stripe-ref",
        name="stripe",
        display_name="Stripe",
        is_enabled=True,
        capabilities=["refund_payment"],
    )
    db_session.add(p_stripe)

    cred = ProviderCredential(
        id="cred-stripe-ref",
        provider_id="p-stripe-ref",
        label="Stripe Key",
        key_prefix="sk_live",
        encrypted_secret=vault.encrypt("sk_live_test_1234"),
        secret_hash="hash_stripe_ref",
        priority=100,
        is_active=True,
        health_status="healthy",
    )
    db_session.add(cred)
    await db_session.commit()

    with pytest.raises(PermissionError) as exc_info:
        await TokenRouterService.execute_route(
            db=db_session,
            consumer=consumer,
            capability="refund_payment",
            model="default",
            request_payload={"payment_intent": "pi_12345", "amount": 1000},
            approval_id=None,
        )

    assert "requires explicit human/admin approval" in str(exc_info.value)


@pytest.mark.asyncio
async def test_refund_payment_lifecycle_and_replay_prevention(db_session):
    """Full lifecycle: create pending -> unapproved rejected -> approved succeeded -> replayed rejected."""
    consumer = Consumer(
        id="c-refund-appr",
        name="test_refund_approved_client",
        is_active=True,
        allowed_capabilities=["refund_payment"],
        allowed_providers=["*"],
    )
    db_session.add(consumer)

    p_stripe = Provider(
        id="p-stripe-ref2",
        name="stripe",
        display_name="Stripe",
        is_enabled=True,
        capabilities=["refund_payment"],
    )
    db_session.add(p_stripe)

    cred = ProviderCredential(
        id="cred-stripe-ref2",
        provider_id="p-stripe-ref2",
        label="Stripe Key",
        key_prefix="sk_live",
        encrypted_secret=vault.encrypt("sk_live_test_5678"),
        secret_hash="hash_stripe_ref2",
        priority=100,
        is_active=True,
        health_status="healthy",
    )
    db_session.add(cred)
    await db_session.commit()

    # 1. Create pending approval
    pending = ApprovalManager.create_pending_operation(
        risk_level="high",
        action_type="refund_payment",
        description="Customer refund for order #9876",
        requester=consumer.name,
        target_id="pi_9876",
    )
    approval_id = pending.operation_id

    # 2. Try executing before approval -> fails
    with pytest.raises(PermissionError) as unapproved_err:
        await TokenRouterService.execute_route(
            db=db_session,
            consumer=consumer,
            capability="refund_payment",
            model="default",
            request_payload={"payment_intent": "pi_9876", "amount": 2500},
            approval_id=approval_id,
        )
    assert "has not been approved" in str(unapproved_err.value)

    # 3. Approve operation
    ApprovalManager.approve_operation(operation_id=approval_id, approver="security_officer_alice")

    # 4. Execute with valid approval -> succeeds
    with patch("token_router.adapters.stripe_adapter.StripeAdapter.execute_operation") as mock_exec:
        mock_exec.return_value = (200, {"id": "re_12345", "status": "succeeded"}, {"transactions": 1})

        res = await TokenRouterService.execute_route(
            db=db_session,
            consumer=consumer,
            capability="refund_payment",
            model="default",
            request_payload={"payment_intent": "pi_9876", "amount": 2500},
            approval_id=approval_id,
        )

        assert res["status"] == "success"
        assert res["data"]["id"] == "re_12345"

    # 5. Try REPLAYING the exact same approval -> fails with replay protection!
    with pytest.raises(RuntimeError) as replay_err:
        await TokenRouterService.execute_route(
            db=db_session,
            consumer=consumer,
            capability="refund_payment",
            model="default",
            request_payload={"payment_intent": "pi_9876", "amount": 2500},
            approval_id=approval_id,
        )
    assert "has already been consumed" in str(replay_err.value)
