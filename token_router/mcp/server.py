"""Official Model Context Protocol (MCP) Server for Weirwise Token Router.

Universal Agent Access Layer: Allows AI Agents to discover capabilities,
inspect health, execute requests across AI, Communication, Payments, and Storage,
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

    def __init__(self, name: str = "weirwise-token-router", version: str = "2.0.0"):
        self.name = name
        self.version = version

    def get_tool_manifest(self) -> List[Dict[str, Any]]:
        """Returns tool schema definitions conforming to MCP standard."""
        return [
            {
                "name": "discover_capabilities",
                "description": "Discover all universal service categories (AI, Communication, Payments, Storage, Data), operations, and providers.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "category_filter": {
                            "type": "string",
                            "description": "Optional category filter: 'ai', 'communication', 'payments', 'storage', 'data'",
                        }
                    },
                },
            },
            {
                "name": "run_diagnostics",
                "description": "Runs automated self-diagnostics on Token Router health, DB connectivity, vault status, and categories.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "execute_request",
                "description": "Routes and executes an operation across any category (AI chat, email, payment, SMS, object storage) using intelligent failover.",
                "inputSchema": {
                    "type": "object",
                    "required": ["capability", "request"],
                    "properties": {
                        "capability": {
                            "type": "string",
                            "description": "Operation or capability ID (e.g. 'chat', 'send_email', 'create_payment_intent', 'send_sms', 'put_object')",
                        },
                        "provider": {"type": "string", "description": "Specific provider e.g. 'openai', 'stripe', 'twilio' or omit for auto"},
                        "model": {"type": "string", "description": "Model or operation target, or 'auto'"},
                        "request": {"type": "object", "description": "Operation payload dictionary"},
                        "strategy": {"type": "string", "description": "priority, round_robin, weighted, lowest_cost"},
                        "idempotency_key": {"type": "string", "description": "Optional unique idempotency key"},
                    },
                },
            },
            {
                "name": "list_credentials_safe",
                "description": "Lists masked credential metadata and health status across all services. Zero secret keys exposed.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "provider_id": {"type": "string", "description": "Optional provider ID filter"}
                    },
                },
            },
            {
                "name": "request_sensitive_operation",
                "description": "Creates an authorization request for sensitive/high-risk actions (e.g. key deletion, budget increase, refund).",
                "inputSchema": {
                    "type": "object",
                    "required": ["action_type", "description", "requester"],
                    "properties": {
                        "action_type": {"type": "string", "description": "Action type e.g. 'delete_credential', 'refund_payment'"},
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
                return await TokenRouterService.discover_capabilities(
                    db=db,
                    category_filter=arguments.get("category_filter"),
                )

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
                    "operation_id": op.operation_id,
                    "status": "pending_approval",
                    "message": "Approval request queued",
                }

            elif tool_name == "execute_request":
                # For local MCP execution, construct a default internal consumer if not found
                mock_consumer = Consumer(
                    id="mcp-agent-session",
                    name="mcp_autonomous_agent",
                    is_active=True,
                    allowed_capabilities=["*"],
                    allowed_providers=["*"],
                )
                res = await TokenRouterService.execute_route(
                    db=db,
                    consumer=mock_consumer,
                    capability=arguments["capability"],
                    model=arguments.get("model", "auto"),
                    request_payload=arguments["request"],
                    provider_name=arguments.get("provider"),
                    strategy=arguments.get("strategy"),
                    idempotency_key=arguments.get("idempotency_key"),
                )
                return res

            else:
                raise ValueError(f"Unknown tool: {tool_name}")

    async def handle_rpc_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handles JSON-RPC 2.0 messages."""
        msg_id = message.get("id")
        method = message.get("method")

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": self.version},
                },
            }

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": self.get_tool_manifest()},
            }

        elif method == "tools/call":
            params = message.get("params", {})
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            try:
                res = await self.handle_tool_call(tool_name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(res, default=str)}]},
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32000, "message": str(e)},
                }

        elif method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        return None

    async def run_stdio(self):
        """Standard I/O asynchronous event loop for Agent connectivity."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8").strip())
                response = await self.handle_rpc_message(msg)
                if response:
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()
            except Exception as e:
                err_resp = {"jsonrpc": "2.0", "error": {"code": -32700, "message": str(e)}}
                sys.stdout.write(json.dumps(err_resp) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    server = MCPServer()
    asyncio.run(server.run_stdio())
