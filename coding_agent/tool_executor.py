# -*- coding: utf-8 -*-
"""
执行与收集解耦的工具调用。
类似 claude-code-cli 中的 StreamingToolExecutor，负责将 Message 中的 ToolUseBlock 分发、执行，最后并入 Context。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from .config import CFG
from .hooks import HookManager
from .llm.base import ToolCall
from .utils.log import log, preview_text
from .utils.metrics import METRICS

if TYPE_CHECKING:
    from .agent_runtime import AgentCallbacks
    from .context.compactor import Compactor
    from .context.window import ContextWindow
    from .tools.registry import ToolRegistry


class ToolExecutor:
    """管理并发或串行执行一组 ToolCall 的管理器。"""

    def __init__(
        self,
        tools: ToolRegistry,
        callbacks: AgentCallbacks,
        hooks: HookManager,
        window: ContextWindow,
        compactor: Compactor,
    ):
        self.tools = tools
        self.callbacks = callbacks
        self.hooks = hooks
        self.window = window
        self.compactor = compactor
        self._executor = ThreadPoolExecutor(max_workers=4)

    def execute_all(self, tool_calls: list[ToolCall]) -> list[str]:
        """批量执行，根据 parallel 分别抛给线程池或顺序执行。"""
        if len(tool_calls) == 1:
            return [self.execute_single(tool_calls[0])]

        parallel_group: list[tuple[int, ToolCall]] = []
        serial_group: list[tuple[int, ToolCall]] = []

        for i, tc in enumerate(tool_calls):
            td = self.tools.get(tc.name)
            if td and td.parallel:
                parallel_group.append((i, tc))
            else:
                serial_group.append((i, tc))

        results: list[str] = [""] * len(tool_calls)

        if parallel_group:
            futures = {
                i: self._executor.submit(self.execute_single, tc)
                for i, tc in parallel_group
            }
            for i, future in futures.items():
                results[i] = future.result(timeout=300)

        for i, tc in serial_group:
            results[i] = self.execute_single(tc)

        return results

    def execute_single(self, tc: ToolCall) -> str:
        """执行单个工具调用：Hook -> MCP Permissions -> Execute -> Hook。"""
        tool_input = dict(tc.arguments)
        ctx = {"tool_name": tc.name, "tool_input": tool_input}

        pre = self.hooks.run_hooks("PreToolUse", ctx)
        if pre.blocked:
            reason = pre.block_reason or "Blocked by hook"
            log("WARN", "hook_blocked", f"{tc.name}: {reason}")
            if self.callbacks.on_system:
                self.callbacks.on_system(f"[Hook] 工具 {tc.name} 被拦截: {reason}")
            return f"Tool blocked by PreToolUse hook: {reason}"

        if pre.updated_input is not None:
            tool_input = pre.updated_input
            tc = ToolCall(id=tc.id, name=tc.name, arguments=tool_input)
            log("INFO", "hook_updated_input",
                f"{tc.name}: {preview_text(json.dumps(tool_input, ensure_ascii=False), 80)}")

        if tc.name.startswith("mcp__"):
            from ..mcp.permission import get_permission_gate
            gate = get_permission_gate()
            decision = gate.check(tc.name, tool_input)
            if decision["behavior"] == "ask":
                intent = decision["intent"]
                preview = json.dumps(tool_input, ensure_ascii=False)[:400]
                src = (
                    f"mcp:{intent.get('server')}/{intent['tool']}"
                    if intent.get("server")
                    else intent["tool"]
                )
                q = (
                    f"[MCP 权限] 是否允许执行 {src} (risk={intent['risk']})？\n"
                    f"参数预览:\n{preview}\n\n回复 yes 确认，其它取消。"
                )
                if self.callbacks.ask_user:
                    ans = self.callbacks.ask_user(q, "n").strip().lower()
                else:
                    from .tools.builtin.user import ask_user as _ask_user_mcp
                    ans = _ask_user_mcp(q, "n").strip().lower()
                if ans not in ("y", "yes"):
                    return f"Permission denied: {decision['reason']}"

        if self.callbacks.on_tool_start:
            self.callbacks.on_tool_start(tc.name, tool_input)
        log("INFO", "tool_call",
            f"name={tc.name} args={preview_text(json.dumps(tool_input, ensure_ascii=False), 120)}")

        t0 = time.perf_counter()

        if tc.name == "compact":
            self.window.messages = self.compactor.compact(
                self.window, focus=tool_input.get("focus"))
            result = "对话已压缩。"
        else:
            result = self.tools.execute(tc.name, tool_input)

        elapsed = (time.perf_counter() - t0) * 1000
        METRICS.record_tool(elapsed)
        log("INFO", "tool_result",
            f"name={tc.name} chars={len(result)} ms={elapsed:.0f}")

        ctx["tool_output"] = result
        post = self.hooks.run_hooks("PostToolUse", ctx)
        for msg in post.messages:
            result += f"\n[Hook note]: {msg}"
        for msg in pre.messages:
            result += f"\n[Hook note]: {msg}"

        if self.callbacks.on_tool_end:
            self.callbacks.on_tool_end(tc.name, preview_text(result))
        if tc.name == "todo" and self.callbacks.on_todo_update:
            self.callbacks.on_todo_update()

        return result
