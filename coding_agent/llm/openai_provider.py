# -*- coding: utf-8 -*-
"""OpenAI / 兼容 API provider。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from .base import Message, StreamChunk, ToolCall, ToolSpec


class OpenAIProvider:
    """基于 openai SDK，支持 OpenAI 及兼容 API（如 DeepSeek）。"""

    def __init__(self, api_key: str, model: str = "gpt-4o", base_url: str | None = None):
        import openai
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self._async_client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    @model_name.setter
    def model_name(self, v: str):
        self._model = v

    def _to_oai_messages(self, messages: list[Message], system: str) -> list[dict]:
        """将 Message 列表转为 Chat Completions 的 messages（含 tool_calls / tool）。"""
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "system":
                out.append({"role": "system", "content": m.content})
            elif m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                d: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
                if m.tool_calls:
                    d["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                        }
                        for tc in m.tool_calls
                    ]
                out.append(d)
            elif m.role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.tool_call_id or "",
                    "content": m.content,
                })
        return out

    def _to_oai_tools(self, tools: list[ToolSpec]) -> list[dict] | None:
        """ToolSpec → OpenAI tools 列表（type=function）。"""
        if not tools:
            return None
        return [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools
        ]

    def _parse_choice(self, choice) -> Message:
        """将 completion choice 转为 Message，并统一 finish_reason 语义。"""
        msg = choice.message
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=json.loads(tc.function.arguments or "{}"),
                )
                for tc in msg.tool_calls
            ]
        result = Message.assistant(content=msg.content or "", tool_calls=tool_calls)
        # 映射 finish_reason: "length" → "max_tokens"
        fr = getattr(choice, "finish_reason", "") or ""
        if fr == "length":
            result.finish_reason = "max_tokens"
        elif fr == "stop":
            result.finish_reason = "end_turn"
        elif fr == "tool_calls":
            result.finish_reason = "tool_use"
        else:
            result.finish_reason = fr
        return result

    def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        system: str = "",
    ) -> Message:
        """同步 chat.completions，附加 usage 到消息对象。"""
        oai_msgs = self._to_oai_messages(messages, system)
        kwargs: dict[str, Any] = {"model": self._model, "messages": oai_msgs}
        oai_tools = self._to_oai_tools(tools or [])
        if oai_tools:
            kwargs["tools"] = oai_tools
        resp = self._client.chat.completions.create(**kwargs)
        msg = self._parse_choice(resp.choices[0])
        msg._usage = resp.usage  # type: ignore[attr-defined]
        return msg

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        system: str = "",
    ) -> AsyncIterator[StreamChunk]:
        """流式增量；结束时输出完整 tool_call StreamChunk。"""
        oai_msgs = self._to_oai_messages(messages, system)
        kwargs: dict[str, Any] = {"model": self._model, "messages": oai_msgs, "stream": True}
        oai_tools = self._to_oai_tools(tools or [])
        if oai_tools:
            kwargs["tools"] = oai_tools
        stream = await self._async_client.chat.completions.create(**kwargs)
        # 累积工具调用片段
        pending_calls: dict[int, dict] = {}
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue
            if delta.content:
                yield StreamChunk(type="text", text=delta.content)
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in pending_calls:
                        pending_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    pc = pending_calls[idx]
                    if tc_delta.id:
                        pc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            pc["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            pc["arguments"] += tc_delta.function.arguments
            if chunk.choices[0].finish_reason:
                break
        # 输出完整的工具调用
        for idx in sorted(pending_calls):
            pc = pending_calls[idx]
            try:
                args = json.loads(pc["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id=pc["id"], name=pc["name"], arguments=args),
            )
        yield StreamChunk(type="done")

    def count_tokens(self, text: str) -> int:
        """优先 tiktoken，否则按字符粗估。"""
        try:
            import tiktoken
            enc = tiktoken.encoding_for_model(self._model)
            return len(enc.encode(text))
        except Exception:
            return len(text) // 4
