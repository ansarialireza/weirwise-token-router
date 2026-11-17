"""Tests for CapabilityRegistry and PersistentApprovalManager."""

import pytest
from token_router.core.capabilities import CapabilityRegistry
from token_router.models.entities import Consumer
from token_router.services.approvals import PersistentApprovalManager


def test_capability_registry():
    discovery = CapabilityRegistry.discover()
    assert "services" in discovery
    assert "providers" in discovery
    assert len(discovery["services"]) >= 5
    assert len(discovery["providers"]) >= 7

    chat_service = CapabilityRegistry.get_service("chat")
    assert chat_service is not None
    assert "openai" in chat_service.supported_providers

    openai_prov = CapabilityRegistry.get_provider("openai")
    assert openai_prov is not None
    assert "chat" in openai_prov.supported_services
    assert openai_prov.streaming_supported is True


@pytest.mark.asyncio
async def test_persistent_approval_manager_flow(db_session):
    consumer = Consumer(
        id="c-appr-flow",
        name="test_agent_flow",
        is_active=True,
    )
    db_session.add(consumer)
    await db_session.commit()

    payload = {"key_id": "cred-12345"}
    req = await PersistentApprovalManager.create_request(
        db=db_session,
        consumer_id=consumer.id,
        risk_level="high",
        action_type="delete_credential",
        description="Purging revoked OpenAI backup key",
        requester="ai_agent_007",
        payload=payload,
        target_id="cred-12345",
    )
    assert req.id is not None
    assert req.is_approved is False
    assert req.status == "pending"

    # Retrieve
    found = await PersistentApprovalManager.get_request(db_session, req.id)
    assert found is not None
    assert found.action_type == "delete_credential"

    # Approve
    approved = await PersistentApprovalManager.approve(db_session, req.id, approver="staff_admin")
    assert approved.is_approved is True
    assert approved.approved_by == "staff_admin"
    assert approved.status == "approved"
