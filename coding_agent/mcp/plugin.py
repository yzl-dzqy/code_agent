# -*- coding: utf-8 -*-
"""
插件清单：从 workdir/.claude-plugin/plugin.json 读取 mcpServers 配置。

清单本身不是 MCP 进程；仅用于声明要启动的命令与参数。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..utils.log import log


class PluginLoader:
    def __init__(self, search_dirs: list[Path] | None = None):
        self.search_dirs = search_dirs or []
        self.plugins: dict[str, dict[str, Any]] = {}

    def scan(self) -> list[str]:
        found: list[str] = []
        for search_dir in self.search_dirs:
            plugin_dir = Path(search_dir) / ".claude-plugin"
            manifest_path = plugin_dir / "plugin.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                name = manifest.get("name", plugin_dir.parent.name)
                self.plugins[name] = manifest
                found.append(name)
            except (json.JSONDecodeError, OSError) as e:
                log("WARN", "plugin_manifest", f"{manifest_path}: {e}")
        return found

    def get_mcp_servers(self) -> dict[str, dict[str, Any]]:
        """{plugin__server_name: {command, args, env}}"""
        servers: dict[str, dict[str, Any]] = {}
        for plugin_name, manifest in self.plugins.items():
            mcp = manifest.get("mcpServers") or {}
            if not isinstance(mcp, dict):
                continue
            for server_name, config in mcp.items():
                if isinstance(config, dict):
                    key = f"{plugin_name}__{server_name}"
                    servers[key] = config
        return servers
