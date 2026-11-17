"""Tests for MCP Protocol Tool Manifest & Handlers."""

import pytest
from token_router.mcp.server import MCPServer


@pytest.mark.asyncio
async def test_mcp_server_manifest():
    server = MCPServer()
    tools = server.get_tool_manifest()
    tool_names = [t["name"] for t in tools]
    assert "discover_capabilities" in tool_names
    assert "execute_request" in tool_names
    assert "run_diagnostics" in tool_names
    assert "list_credentials_safe" in tool_names
    assert "request_sensitive_operation" in tool_names


@pytest.mark.asyncio
async def test_mcp_server_discover_and_diagnostics():
    server = MCPServer()
    discovery = await server.handle_tool_call("discover_capabilities", {})
    assert "services" in discovery
    assert "providers" in discovery

    diagnostics = await server.handle_tool_call("run_diagnostics", {})
    assert diagnostics["status"] == "healthy"
    assert "database_connection" in diagnostics["checks"]


@pytest.mark.asyncio
async def test_mcp_server_sensitive_operation_approval_guard():
    server = MCPServer()
    res = await server.handle_tool_call(
        "request_sensitive_operation",
        {
            "action_type": "rotate_master_key",
            "description": "Routine key rotation",
            "requester": "autonomous_security_agent",
        },
    )
    assert res["status"] == "pending_approval"
    assert "operation_id" in res
