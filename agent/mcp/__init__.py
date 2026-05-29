# -*- coding: utf-8 -*-
"""MCP 客户端、插件清单与工具路由。"""

from .integration import MCPInitResult, init_mcp_for_registry, shutdown_mcp
from .permission import CapabilityPermissionGate, get_permission_gate
from .router import MCP_ROUTER, MCPToolRouter

__all__ = [
    "MCPInitResult",
    "MCPToolRouter",
    "MCP_ROUTER",
    "CapabilityPermissionGate",
    "get_permission_gate",
    "init_mcp_for_registry",
    "shutdown_mcp",
]
