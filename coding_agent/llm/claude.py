# -*- coding: utf-8 -*-
"""Anthropic Claude provider。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from .base import Message, StreamChunk, ToolCall, ToolSpec


class ClaudeProvider:
    """基于 anthropic SDK。"""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._async_client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    @model_name.setter
    def model_name(self, v: str):
        self._model = v

    def _to_claude_messages(self, messages: list[Message]) -> list[dict]:
        """将统一 Message 转为 Claude Messages API 的 messages 列表（含 tool_use / tool_result）。"""
        out: list[dict] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                content: list[dict] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        content.append({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        })
                out.append({"role": "assistant", "content": content or m.content})
            elif m.role == "tool":
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id or "",
                        "content": m.content,
                    }],
                })
        return out

    def _to_claude_tools(self, tools: list[ToolSpec]) -> list[dict]:
        """ToolSpec → Claude tools 参数（input_schema）。"""
        return [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]

    def _get_system(self, messages: list[Message], system: str) -> str:
        """合并显式 system 参数与消息中的 role=system 段落。"""
        parts = [system] if system else []
        for m in messages:
            if m.role == "system":
                parts.append(m.content)
        return "\n\n".join(parts) if parts else ""

    def _parse_response(self, resp) -> Message:
        """解析非流式 messages.create 响应为 Message，并挂上 usage。"""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input or {}))
        msg = Message.assistant("\n".join(text_parts), tool_calls=tool_calls or None)
        msg._usage = resp.usage  # type: ignore[attr-defined]
        # Claude stop_reason: "end_turn" / "max_tokens" / "tool_use"
        msg.finish_reason = getattr(resp, "stop_reason", "") or ""
        return msg

    def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        system: str = "",
    ) -> Message:
        """同步非流式对话，返回完整 assistant 消息。"""
        kwargs = {
            "model": self._model,
            "messages": self._to_claude_messages(messages),
            "max_tokens": 8192,
        }
        sys_text = self._get_system(messages, system)
        if sys_text:
            kwargs["system"] = sys_text
        if tools:
            kwargs["tools"] = self._to_claude_tools(tools)
        resp = self._client.messages.create(**kwargs)
        return self._parse_response(resp)

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        system: str = "",
    ) -> AsyncIterator[StreamChunk]:
        """流式输出文本增量与完整 tool_call chunk。"""
        kwargs = {
            "model": self._model,
            "messages": self._to_claude_messages(messages),
            "max_tokens": 8192,
            "stream": True,
        }
        sys_text = self._get_system(messages, system)
        if sys_text:
            kwargs["system"] = sys_text
        if tools:
            kwargs["tools"] = self._to_claude_tools(tools)

        # 工具调用累积
        current_tool: dict | None = None
        async with self._async_client.messages.stream(**{k: v for k, v in kwargs.items() if k != "stream"}) as stream:
            async for event in stream:
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        current_tool = {"id": block.id, "name": block.name, "input_json": ""}
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield StreamChunk(type="text", text=delta.text)
                    elif delta.type == "input_json_delta" and current_tool is not None:
                        current_tool["input_json"] += delta.partial_json
                elif event.type == "content_block_stop":
                    if current_tool is not None:
                        try:
                            args = json.loads(current_tool["input_json"] or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        yield StreamChunk(
                            type="tool_call",
                            tool_call=ToolCall(
                                id=current_tool["id"],
                                name=current_tool["name"],
                                arguments=args,
                            ),
                        )
                        current_tool = None
        yield StreamChunk(type="done")

    def count_tokens(self, text: str) -> int:
        """优先用官方 count_tokens，失败则按字符粗估。"""
        try:
            resp = self._client.count_tokens(
                model=self._model,
                messages=[{"role": "user", "content": text}],
            )
            return resp.input_tokens
        except Exception:
            return len(text) // 4
