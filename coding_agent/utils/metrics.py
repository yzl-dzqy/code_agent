# -*- coding: utf-8 -*-
"""会话级统计指标。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionMetrics:
    model_turns: int = 0
    tool_invocations: int = 0
    tool_wall_ms: float = 0.0
    user_messages: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0

    def record_model_turn(self) -> None:
        """计一次模型往返（一轮 chat/stream）。"""
        self.model_turns += 1

    def record_tool(self, wall_ms: float) -> None:
        """累计一次工具调用及墙钟耗时（毫秒）。"""
        self.tool_invocations += 1
        self.tool_wall_ms += wall_ms

    def record_tokens(self, prompt: int, output: int) -> None:
        """累加 prompt / 输出 token 用量。"""
        self.prompt_tokens += prompt
        self.output_tokens += output

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def summary_line(self) -> str:
        """单行可读的会话统计摘要。"""
        return (
            f"model_turns={self.model_turns} tools={self.tool_invocations} "
            f"tool_ms={self.tool_wall_ms:.0f} tokens={self.total_tokens}"
            f"(in={self.prompt_tokens} out={self.output_tokens})"
        )


METRICS = SessionMetrics()
RUN_SESSION_ID: str = ""
