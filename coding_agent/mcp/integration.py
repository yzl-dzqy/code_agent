# -*- coding: utf-8 -*-
"""
启动时：扫描插件 → 连接 MCP → 把外部工具注册进 ToolRegistry。

内置工具同名时优先保留内置（不覆盖）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import CFG
from ..tools.base import ToolDef
from ..tools.registry import ToolRegistry
from ..utils.log import log
from .client import MCPClient
from .permission import reset_permission_gate
from .plugin import PluginLoader
from .router import MCP_ROUTER, MCPToolRouter


@dataclass
class MCPInitResult:
    plugin_names: list[str]
    connected_servers: list[str]
    tool_count: int


def shutdown_mcp(router: MCPToolRouter | None = None) -> None:
    r = router or MCP_ROUTER
    for c in list(r.clients.values()):
        try:
            c.disconnect()
        except Exception:
            pass
    r.clients.clear()
    reset_permission_gate()


def init_mcp_for_registry(
    registry: ToolRegistry,
    *,
    router: MCPToolRouter | None = None,
    search_dirs: list | None = None,
) -> MCPInitResult:
    """
    根据配置连接 MCP，并把工具注册到 registry。

    默认使用全局 MCP_ROUTER；可传入 router 便于测试。
    """
    shutdown_mcp(router)
    reset_permission_gate()

    if not getattr(CFG, "mcp_enabled", True):
        log("INFO", "mcp", "disabled by config")
        return MCPInitResult([], [], 0)

    r = router or MCP_ROUTER
    dirs = [CFG.workdir] if search_dirs is None else list(search_dirs)
    loader = PluginLoader(dirs)
    found = loader.scan()
    connected: list[str] = []

    for server_name, config in loader.get_mcp_servers().items():
        cmd = str(config.get("command") or "").strip()
        args = config.get("args") or []
        if not isinstance(args, list):
            args = []
        env = config.get("env")
        env_map = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else None
        client = MCPClient(server_name, cmd, [str(a) for a in args], env_map)
        if client.connect():
            client.list_tools()
            r.register_client(client)
            connected.append(server_name)
            log("INFO", "mcp_connected", f"{server_name} ({len(client._tools)} tools)")
        else:
            log("WARN", "mcp_skipped", server_name)

    native_names = set(registry.names)
    added = 0
    for spec in r.get_all_agent_tools():
        name = spec["name"]
        if name in native_names:
            log("DEBUG", "mcp_skip_native_conflict", name)
            continue
        schema = spec["input_schema"]
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        req = list(schema.get("required") or [])

        def _make_invoke(pref: str):
            def _invoke(**kwargs: object) -> str:
                return r.call(pref, dict(kwargs))

            return _invoke

        registry.register(
            ToolDef(
                name=name,
                description=spec.get("description", "[MCP]"),
                fn=_make_invoke(name),
                parameters=schema,
                parallel=True,
                required_params=req,
            )
        )
        native_names.add(name)
        added += 1

    log("INFO", "mcp_tools_registered", f"{added} tools from MCP")
    return MCPInitResult(found, connected, added)
