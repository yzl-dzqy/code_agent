# -*- coding: utf-8 -*-
"""滑动窗口上下文管理器。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm.base import Message
from .tokenizer import count_messages_tokens


@dataclass
class ContextWindow:
    """管理对话历史，跟踪 token 用量，触发压缩。"""
    messages: list[Message] = field(default_factory=list)
    recent_files: list[str] = field(default_factory=list)
    has_compacted: bool = False
    last_summary: str = ""

    def add(self, msg: Message) -> None:
        """追加一条消息到窗口末尾。"""
        self.messages.append(msg)

    def token_count(self) -> int:
        return count_messages_tokens(self.messages)

    def needs_compaction(self, limit: int) -> bool:
        return self.token_count() > limit

    def track_file(self, path: str) -> None:
        """记录最近访问文件（最多保留 5 个，最新在后）。"""
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.append(path)
        if len(self.recent_files) > 5:
            self.recent_files[:] = self.recent_files[-5:]

    def micro_compact(self, keep_recent: int = 6) -> None:
        """
        轻量压缩：仅替换超长 tool 消息为占位符，保留最近 keep_recent 条完整结果。

        在完整 compact 之前先瘦身，降低 token 又不丢近期上下文。
        """
        tool_indices = [
            i for i, m in enumerate(self.messages)
            if m.role == "tool" and len(m.content) > 120
        ]
        if len(tool_indices) <= keep_recent:
            return
        for idx in tool_indices[:-keep_recent]:
            self.messages[idx] = Message.tool_result(
                tool_call_id=self.messages[idx].tool_call_id or "",
                name=self.messages[idx].name or "",
                content="[较早的工具结果已压缩]",
            )
