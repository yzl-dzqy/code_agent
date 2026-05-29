# -*- coding: utf-8 -*-
"""Agent runtime primitives: callbacks, events, and per-run state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class AgentCallbacks:
    """
    UI 层注入的回调集合。

    Agent 本身不依赖任何 UI 框架，通过此 dataclass 解耦：
      on_text       ← 最终回复文本（流式片段）
      on_tool_start ← 工具调用开始（名称 + 参数）
      on_tool_end   ← 工具调用完成（名称 + 结果摘要）
      on_thinking   ← 模型思考过程
      on_todo_update← 待办看板变更通知
      on_system     ← 系统级消息（恢复提示等）
      ask_user      ← 向用户追问（阻塞等待回复）
    """

    on_text: Callable[[str], None] | None = None
    on_tool_start: Callable[[str, dict], None] | None = None
    on_tool_end: Callable[[str, str], None] | None = None
    on_thinking: Callable[[str], None] | None = None
    on_todo_update: Callable[[], None] | None = None
    on_system: Callable[[str], None] | None = None
    ask_user: Callable[[str, str], str] | None = None


@dataclass(frozen=True)
class AgentEvent:
    """Typed event emitted by the agent loop."""

    type: Literal["text", "system"]
    content: str

    def __getitem__(self, key: str) -> str:
        if key == "type":
            return self.type
        if key == "content":
            return self.content
        raise KeyError(key)


@dataclass
class QueryState:
    """单次 Query 执行的跨 turn 状态。"""

    turn_count: int = 1
    max_output_tokens_recovery_count: int = 0
    stop_hook_active: bool = False
    transition_reason: str = ""
    discovered_tools: set[str] = field(default_factory=set)
