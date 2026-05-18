# -*- coding: utf-8 -*-
"""LLM 统一抽象：Message / ToolCall / ToolSpec / StreamChunk + LLMProvider Protocol。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass
class ToolCall:
    """模型发起的工具调用。"""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolSpec:
    """传给 LLM 的工具定义。"""
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    parallel: bool = True       # 是否可并行执行


@dataclass
class Message:
    """统一消息格式，各 provider 内部转换为自己的 SDK 格式。"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    # role="tool" 时填写
    tool_call_id: str | None = None
    name: str | None = None
    # 模型停止原因（"end_turn" / "max_tokens" / "tool_use" 等）
    finish_reason: str = ""

    @staticmethod
    def system(content: str) -> Message:
        return Message(role="system", content=content)

    @staticmethod
    def user(content: str) -> Message:
        return Message(role="user", content=content)

    @staticmethod
    def assistant(content: str = "", tool_calls: list[ToolCall] | None = None) -> Message:
        return Message(role="assistant", content=content, tool_calls=tool_calls)

    @staticmethod
    def tool_result(tool_call_id: str, name: str, content: str) -> Message:
        return Message(role="tool", content=content, tool_call_id=tool_call_id, name=name)


@dataclass
class StreamChunk:
    """流式输出的单个片段。"""
    type: Literal["text", "tool_call", "done", "error"]
    text: str = ""
    tool_call: ToolCall | None = None
    finish_reason: str = ""


@runtime_checkable
class LLMProvider(Protocol):
    """所有模型提供者的统一接口。"""

    @property
    def model_name(self) -> str:
        """当前模型名。"""
        ...

    def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        system: str = "",
    ) -> Message:
        """同步对话，返回完整的助手消息。"""
        ...

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        system: str = "",
    ) -> AsyncIterator[StreamChunk]:
        """流式对话，逐 chunk 返回。"""
        ...

    def count_tokens(self, text: str) -> int:
        """估算文本 token 数。"""
        ...
