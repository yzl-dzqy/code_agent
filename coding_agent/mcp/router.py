# -*- coding: utf-8 -*-
"""将 mcp__{server}__{tool} 调用分发到对应 MCPClient。"""

from __future__ import annotations

from typing import Any

from .client import MCPClient


class MCPToolRouter:
    def __init__(self) -> None:
        self.clients: dict[str, MCPClient] = {}

    def register_client(self, client: MCPClient) -> None:
        self.clients[client.server_name] = client

    @staticmethod
    def is_mcp_tool(tool_name: str) -> bool:
        return tool_name.startswith("mcp__")

    def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        parts = tool_name.split("__", 2)
        if len(parts) != 3:
            return f"Error: Invalid MCP tool name: {tool_name}"
        _, server_name, actual_tool = parts
        client = self.clients.get(server_name)
        if not client:
            return f"Error: MCP server not found: {server_name}"
        return client.call_tool(actual_tool, arguments)

    def get_all_agent_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for client in self.clients.values():
            tools.extend(client.get_agent_tools())
        return tools


# 进程级路由器（bootstrap 挂载工具时绑定）
MCP_ROUTER = MCPToolRouter()
