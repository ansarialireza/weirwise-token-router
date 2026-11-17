"""Tests for CapabilityRegistry and ApprovalManager."""

import pytest
from token_router.core.capabilities import CapabilityRegistry
from token_router.services.approvals import ApprovalManager


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


def test_approval_manager_flow():
    op = ApprovalManager.create_pending_operation(
        risk_level="high",
        action_type="delete_credential",
        description="Purging revoked OpenAI backup key",
        requester="ai_agent_007",
        target_id="cred-12345",
    )
    assert op.operation_id is not None
    assert op.is_approved is False

    # Retrieve
    found = ApprovalManager.get_operation(op.operation_id)
    assert found is not None
    assert found.action_type == "delete_credential"

    # Approve
    approved = ApprovalManager.approve_operation(op.operation_id, approver="staff_admin")
    assert approved.is_approved is True
    assert approved.approved_by == "staff_admin"

    # Mark executed
    ApprovalManager.mark_executed(op.operation_id)
    assert ApprovalManager.get_operation(op.operation_id).executed is True
