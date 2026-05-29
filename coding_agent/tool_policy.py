# -*- coding: utf-8 -*-
"""Explicit tool execution policy: parallelism and permission decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import CFG
from .llm.base import ToolCall
from .mcp.permission import CapabilityPermissionGate, get_permission_gate
from .tools.base import ToolDef


READ_ONLY_TOOLS = {
    "read_file",
    "list_dir",
    "grep",
    "glob",
    "http_get",
    "web_search",
    "web_fetch",
    "load_skill",
    "task_list",
    "task_get",
    "cron_list",
    "worktree_list",
    "tool_search",
}

SERIAL_TOOLS = {
    "ask_user",
    "bash",
    "run_python",
    "write_file",
    "edit_file",
    "multi_edit",
    "patch",
    "compact",
    "todo",
    "task_create",
    "task_update",
    "cron_create",
    "cron_delete",
    "background_start",
    "background_stop",
    "worktree_create",
    "worktree_commit",
    "worktree_merge",
}


@dataclass
class ToolExecutionPolicy:
    """Decides how a tool call should be scheduled and authorized."""

    native_permission_mode: str = CFG.tool_permission_mode

    def can_run_parallel(self, tool_def: ToolDef | None, tool_call: ToolCall) -> bool:
        if tool_def is None or not tool_def.parallel:
            return False
        if tool_call.name in SERIAL_TOOLS:
            return False
        intent = self.intent(tool_call.name, tool_call.arguments)
        return intent["risk"] == "read"

    def check_permission(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if tool_name.startswith("mcp__"):
            return get_permission_gate().check(tool_name, tool_input)
        gate = CapabilityPermissionGate(mode=self.native_permission_mode)
        return gate.check(tool_name, tool_input)

    def intent(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if tool_name in READ_ONLY_TOOLS:
            return {
                "source": "native",
                "server": None,
                "tool": tool_name,
                "risk": "read",
            }
        if tool_name in SERIAL_TOOLS and tool_name != "ask_user":
            return CapabilityPermissionGate(mode=self.native_permission_mode).normalize(
                tool_name,
                tool_input,
            )
        return CapabilityPermissionGate(mode=self.native_permission_mode).normalize(
            tool_name,
            tool_input,
        )
