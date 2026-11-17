"""Official Model Context Protocol (MCP) Server for Weirwise Token Router.

Allows AI Agents to discover capabilities, inspect health, execute requests,
manage providers, and run diagnostics over stdio or HTTP SSE transports.
"""

import asyncio
import json
import sys
from typing import Any, Dict, List, Optional
from token_router.core.capabilities import CapabilityRegistry
from token_router.db.session import async_session_factory
from token_router.models.entities import Consumer, Provider
from token_router.services.approvals import ApprovalManager
from token_router.services.router_service import TokenRouterService


class MCPServer:
    """Agent-Native Model Context Protocol Server."""

    def __init__(self, name: str = "weirwise-token-router", version: str = "1.0.0"):
        self.name = name
        self.version = version

    def get_tool_manifest(self) -> List[Dict[str, Any]]:
        """Returns tool schema definitions conforming to MCP standard."""
        return [
            {
                "name": "discover_capabilities",
                "description": "Discover all supported AI services, models, and providers in the Token Router.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service_filter": {"type": "string", "description": "Optional filter e.g. 'chat' or 'embeddings'"}
                    },
                },
            },
            {
                "name": "run_diagnostics",
                "description": "Runs automated self-diagnostics on Token Router health, DB connectivity, and vaults.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "execute_request",
                "description": "Routes and executes an AI capability (chat, embeddings, etc.) using intelligent failover.",
                "inputSchema": {
                    "type": "object",
                    "required": ["capability", "request"],
                    "properties": {
                        "capability": {"type": "string", "description": "e.g. 'chat', 'embeddings', 'email'"},
                        "provider": {"type": "string", "description": "Specific provider e.g. 'openai' or omit for auto"},
                        "model": {"type": "string", "description": "Model identifier or 'auto'"},
                        "request": {"type": "object", "description": "Payload dict, e.g. {'messages': [...]}"},
                        "strategy": {"type": "string", "description": "priority, round_robin, weighted, lowest_cost"},
                    },
                },
            },
            {
                "name": "list_credentials_safe",
                "description": "Lists masked credential metadata and health status. Zero secret keys exposed.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "provider_id": {"type": "string", "description": "Optional provider ID filter"}
                    },
                },
            },
            {
                "name": "request_sensitive_operation",
                "description": "Creates an authorization request for sensitive/high-risk actions (e.g. key deletion, budget increase).",
                "inputSchema": {
                    "type": "object",
                    "required": ["action_type", "description", "requester"],
                    "properties": {
                        "action_type": {"type": "string", "description": "Action type e.g. 'delete_credential'"},
                        "description": {"type": "string", "description": "Human rationale for action"},
                        "requester": {"type": "string", "description": "Agent or identity requesting change"},
                        "target_id": {"type": "string", "description": "Target entity ID"},
                    },
                },
            },
        ]

    async def handle_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches an MCP tool call to the shared application service layer."""
        async with async_session_factory() as db:
            if tool_name == "discover_capabilities":
                return await TokenRouterService.discover_capabilities(db=db)

            elif tool_name == "run_diagnostics":
                return await TokenRouterService.run_diagnostics(db=db)

            elif tool_name == "list_credentials_safe":
                return {"credentials": await TokenRouterService.list_credentials_safe(db=db, provider_id=arguments.get("provider_id"))}

            elif tool_name == "request_sensitive_operation":
                op = ApprovalManager.create_pending_operation(
                    risk_level="high",
                    action_type=arguments["action_type"],
                    description=arguments["description"],
                    requester=arguments["requester"],
                    target_id=arguments.get("target_id"),
                )
                return {
                    "status": "pending_approval",
                    "operation_id": op.operation_id,
                    "risk_level": op.risk_level,
                    "description": op.description,
                    "message": "Operation registered. A human admin must authorize before execution.",
                }

            elif tool_name == "execute_request":
                mock_consumer = Consumer(
                    id="mcp-agent-session",
                    name="mcp_autonomous_agent",
                    is_active=True,
                    allowed_capabilities=["chat", "embeddings", "email", "speech-to-text", "text-to-speech"],
                    allowed_providers=["*"],
                )
                return await TokenRouterService.execute_route(
                    db=db,
                    consumer=mock_consumer,
                    capability=arguments.get("capability", "chat"),
                    model=arguments.get("model", "auto"),
                    request_payload=arguments.get("request", {}),
                    provider_name=arguments.get("provider"),
                    strategy=arguments.get("strategy"),
                )

            raise ValueError(f"Unknown MCP tool: {tool_name}")

    async def run_stdio(self):
        """Standard I/O JSON-RPC processing loop for Coding Agents."""
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                rpc = json.loads(line)
                method = rpc.get("method")
                req_id = rpc.get("id")

                if method == "initialize":
                    res = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": self.name, "version": self.version},
                        },
                    }
                elif method == "tools/list":
                    res = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"tools": self.get_tool_manifest()},
                    }
                elif method == "tools/call":
                    params = rpc.get("params", {})
                    name = params.get("name")
                    args = params.get("arguments", {})
                    data = await self.handle_tool_call(name, args)
                    res = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"content": [{"type": "text", "text": json.dumps(data)}]},
                    }
                else:
                    res = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}

                sys.stdout.write(json.dumps(res) + "\n")
                sys.stdout.flush()
            except Exception as e:
                err_res = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}
                sys.stdout.write(json.dumps(err_res) + "\n")
                sys.stdout.flush()


mcp_server = MCPServer()

if __name__ == "__main__":
    asyncio.run(mcp_server.run_stdio())
