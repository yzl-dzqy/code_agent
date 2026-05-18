# -*- coding: utf-8 -*-
"""
MCP / 外部工具权限门：与内置工具共用同一套风险分级，不绕过控制面。

native 工具仍由 Hook / 既有逻辑处理；仅 mcp__ 前缀走此 gate。
"""

from __future__ import annotations

import json
from typing import Any

from ..config import CFG

PERMISSION_MODES = ("default", "auto")


class CapabilityPermissionGate:
    """将工具调用规范为 intent，再按 risk +模式决定 allow / ask。"""

    READ_PREFIXES = ("read", "list", "get", "show", "search", "query", "inspect")
    HIGH_RISK_PREFIXES = ("delete", "remove", "drop", "shutdown")

    def __init__(self, mode: str = "default"):
        self.mode = mode if mode in PERMISSION_MODES else "default"

    def normalize(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            if len(parts) == 3:
                _, server_name, actual_tool = parts
            else:
                server_name, actual_tool = "", tool_name
            source = "mcp"
        else:
            server_name = None
            actual_tool = tool_name
            source = "native"
        lowered = actual_tool.lower()
        if actual_tool == "read_file" or lowered.startswith(self.READ_PREFIXES):
            risk = "read"
        elif actual_tool == "bash":
            command = str(tool_input.get("command", ""))
            risk = (
                "high"
                if any(
                    t in command
                    for t in ("rm -rf", "sudo", "shutdown", "reboot")
                )
                else "write"
            )
        elif lowered.startswith(self.HIGH_RISK_PREFIXES):
            risk = "high"
        else:
            risk = "write"
        return {
            "source": source,
            "server": server_name,
            "tool": actual_tool,
            "risk": risk,
        }

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        intent = self.normalize(tool_name, tool_input)
        if intent["risk"] == "read":
            return {"behavior": "allow", "reason": "Read capability", "intent": intent}
        if self.mode == "auto" and intent["risk"] != "high":
            return {
                "behavior": "allow",
                "reason": "Auto mode for non-high-risk capability",
                "intent": intent,
            }
        if intent["risk"] == "high":
            return {
                "behavior": "ask",
                "reason": "High-risk capability requires confirmation",
                "intent": intent,
            }
        return {
            "behavior": "ask",
            "reason": "State-changing capability requires confirmation",
            "intent": intent,
        }


_gate: CapabilityPermissionGate | None = None


def get_permission_gate() -> CapabilityPermissionGate:
    global _gate
    if _gate is None:
        _gate = CapabilityPermissionGate(mode=getattr(CFG, "mcp_permission_mode", "default"))
    return _gate


def reset_permission_gate() -> None:
    """测试或重载配置时重置单例。"""
    global _gate
    _gate = None


def format_tool_result_payload(
    tool_name: str, output: str, intent: dict[str, Any] | None = None,
) -> str:
    """将执行结果包一层 JSON，便于审计（与教学脚本 normalize_tool_result 对齐）。"""
    intent = intent or get_permission_gate().normalize(tool_name, {})
    status = "error" if "Error:" in output or "MCP Error:" in output else "ok"
    payload = {
        "source": intent["source"],
        "server": intent.get("server"),
        "tool": intent["tool"],
        "risk": intent["risk"],
        "status": status,
        "preview": output[:500],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
