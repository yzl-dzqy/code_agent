# -*- coding: utf-8 -*-
"""
最小 stdio MCP客户端：initialize → tools/list → tools/call。

与教学章节一致，采用每行一条 JSON 的消息形式（非 Content-Length 帧）。
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from ..utils.log import log


class MCPClient:
    def __init__(
        self,
        server_name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self.server_name = server_name
        self.command = command
        self.args = args or []
        self.env = {**os.environ, **(env or {})}
        self.process: subprocess.Popen[str] | None = None
        self._request_id = 0
        self._tools: list[dict[str, Any]] = []

    def connect(self) -> bool:
        """启动子进程并完成 initialize / initialized 握手。"""
        try:
            if not self.command:
                log("WARN", "mcp_connect", f"{self.server_name}: empty command")
                return False
            self.process = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                text=True,
            )
            self._send_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "coding-agent", "version": "1.0"},
                },
            )
            response = self._recv()
            if response and "result" in response:
                self._send_notification("notifications/initialized", None)
                return True
        except FileNotFoundError:
            log("ERROR", "mcp_connect", f"command not found: {self.command}")
        except Exception as e:
            log("ERROR", "mcp_connect", str(e))
        return False

    def list_tools(self) -> list[dict[str, Any]]:
        self._send_request("tools/list", {})
        response = self._recv()
        if response and "result" in response:
            self._tools = response["result"].get("tools", [])
        return self._tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        self._send_request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )
        response = self._recv()
        if response and "result" in response:
            content = response["result"].get("content", [])
            return "\n".join(
                str(c.get("text", c)) if isinstance(c, dict) else str(c)
                for c in content
            )
        if response and "error" in response:
            err = response["error"]
            msg = err.get("message", "unknown") if isinstance(err, dict) else str(err)
            return f"MCP Error: {msg}"
        return "MCP Error: no response"

    def get_agent_tools(self) -> list[dict[str, Any]]:
        """转为带 mcp__ 前缀的 agent 工具描述（供 ToolSpec）。"""
        agent_tools: list[dict[str, Any]] = []
        for tool in self._tools:
            raw_name = tool.get("name", "")
            prefixed_name = f"mcp__{self.server_name}__{raw_name}"
            schema = tool.get("inputSchema") or {
                "type": "object",
                "properties": {},
            }
            agent_tools.append({
                "name": prefixed_name,
                "description": tool.get("description", ""),
                "input_schema": schema,
                "_mcp_server": self.server_name,
                "_mcp_tool": raw_name,
            })
        return agent_tools

    def disconnect(self) -> None:
        if not self.process:
            return
        try:
            self._send_request("shutdown", {})
        except Exception:
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        self.process = None

    def _send_request(self, method: str, params: dict[str, Any] | None) -> None:
        if not self.process or self.process.poll() is not None:
            return
        self._request_id += 1
        envelope: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params is not None:
            envelope["params"] = params
        line = json.dumps(envelope, ensure_ascii=False) + "\n"
        try:
            stdin = self.process.stdin
            if stdin:
                stdin.write(line)
                stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _send_notification(self, method: str, params: dict[str, Any] | None) -> None:
        if not self.process or self.process.poll() is not None:
            return
        envelope: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            envelope["params"] = params
        line = json.dumps(envelope, ensure_ascii=False) + "\n"
        try:
            assert self.process.stdin
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _recv(self) -> dict[str, Any] | None:
        if not self.process or self.process.poll() is not None:
            return None
        try:
            assert self.process.stdout
            line = self.process.stdout.readline()
            if line:
                return json.loads(line)
        except (json.JSONDecodeError, OSError):
            pass
        return None
