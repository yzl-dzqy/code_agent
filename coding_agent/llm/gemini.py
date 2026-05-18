# -*- coding: utf-8 -*-
"""Google Gemini provider。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from google import genai

from .base import Message, StreamChunk, ToolCall, ToolSpec


class GeminiProvider:
    """基于 google-genai SDK 的 Gemini 实现。"""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    @model_name.setter
    def model_name(self, v: str):
        self._model = v

    # ── 内部格式转换 ──

    def _to_genai_tools(self, tools: list[ToolSpec]) -> list:
        """将 ToolSpec 列表包装为 Gemini Tool(function_declarations)。"""
        if not tools:
            return []
        decls = []
        for t in tools:
            decls.append(
                genai.types.FunctionDeclaration(
                    name=t.name,
                    description=t.description,
                    parameters=self._schema_to_genai(t.parameters),
                )
            )
        return [genai.types.Tool(function_declarations=decls)]

    def _schema_to_genai(self, schema: dict) -> genai.types.Schema:
        """递归将 JSON Schema dict 转为 genai.types.Schema。"""
        type_map = {
            "object": genai.types.Type.OBJECT,
            "array": genai.types.Type.ARRAY,
            "string": genai.types.Type.STRING,
            "integer": genai.types.Type.INTEGER,
            "number": genai.types.Type.NUMBER,
            "boolean": genai.types.Type.BOOLEAN,
        }
        t = type_map.get((schema.get("type") or "string").lower(), genai.types.Type.STRING)
        kw: dict[str, Any] = {"type": t}
        if "description" in schema:
            kw["description"] = schema["description"]
        if "enum" in schema:
            kw["enum"] = schema["enum"]
        if "properties" in schema:
            kw["properties"] = {k: self._schema_to_genai(v) for k, v in schema["properties"].items()}
        if schema.get("required"):
            kw["required"] = schema["required"]
        if "items" in schema:
            kw["items"] = self._schema_to_genai(schema["items"])
        elif t == genai.types.Type.ARRAY:
            # Gemini 要求 array 必须有 items
            kw["items"] = genai.types.Schema(type=genai.types.Type.STRING)
        return genai.types.Schema(**kw)

    def _to_genai_contents(self, messages: list[Message]) -> list:
        """将统一 Message 列表转为 Gemini SDK 的 contents 列表。"""
        contents = []
        for msg in messages:
            if msg.role == "system":
                continue  # system 通过 system_instruction 传入
            if msg.role == "user":
                contents.append(genai.types.Content(
                    role="user",
                    parts=[genai.types.Part(text=msg.content)],
                ))
            elif msg.role == "assistant":
                parts = []
                if msg.content:
                    parts.append(genai.types.Part(text=msg.content))
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        parts.append(genai.types.Part.from_function_call(
                            name=tc.name, args=tc.arguments,
                        ))
                if parts:
                    contents.append(genai.types.Content(role="model", parts=parts))
            elif msg.role == "tool":
                contents.append(genai.types.Content(
                    role="user",
                    parts=[genai.types.Part.from_function_response(
                        name=msg.name or "",
                        response={"result": msg.content},
                    )],
                ))
        return contents

    def _parse_response(self, response) -> Message:
        """将 Gemini 响应转为统一 Message。"""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        finish = ""
        for candidate in getattr(response, "candidates", []) or []:
            # 提取 finish_reason
            fr = getattr(candidate, "finish_reason", None)
            if fr:
                raw = str(fr).lower()
                if "max_tokens" in raw or "length" in raw:
                    finish = "max_tokens"
                elif "stop" in raw:
                    finish = "end_turn"
            for part in getattr(candidate.content, "parts", []) or []:
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    tool_calls.append(ToolCall(
                        id=str(getattr(fc, "id", None) or uuid.uuid4().hex[:8]),
                        name=fc.name,
                        arguments=dict(fc.args or {}),
                    ))
                elif hasattr(part, "text") and part.text:
                    text_parts.append(part.text)
        msg = Message.assistant(
            content="\n".join(text_parts),
            tool_calls=tool_calls or None,
        )
        msg.finish_reason = finish
        return msg

    # ── 公开接口 ──

    def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        system: str = "",
    ) -> Message:
        """generate_content 非流式，写入 usage_metadata。"""
        cfg = genai.types.GenerateContentConfig(
            system_instruction=system or None,
            tools=self._to_genai_tools(tools or []) or None,
        )
        contents = self._to_genai_contents(messages)
        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=cfg,
        )
        usage = getattr(response, "usage_metadata", None)
        msg = self._parse_response(response)
        msg._usage = usage  # type: ignore[attr-defined]
        return msg

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        system: str = "",
    ) -> AsyncIterator[StreamChunk]:
        """generate_content_stream：逐段 text 与 function_call。"""
        cfg = genai.types.GenerateContentConfig(
            system_instruction=system or None,
            tools=self._to_genai_tools(tools or []) or None,
        )
        contents = self._to_genai_contents(messages)
        # Gemini SDK 流式接口
        accumulated_calls: dict[int, dict] = {}
        for chunk in self._client.models.generate_content_stream(
            model=self._model, contents=contents, config=cfg,
        ):
            for candidate in getattr(chunk, "candidates", []) or []:
                for part in getattr(candidate.content, "parts", []) or []:
                    if hasattr(part, "text") and part.text:
                        yield StreamChunk(type="text", text=part.text)
                    elif hasattr(part, "function_call") and part.function_call:
                        fc = part.function_call
                        tc = ToolCall(
                            id=str(getattr(fc, "id", None) or uuid.uuid4().hex[:8]),
                            name=fc.name,
                            arguments=dict(fc.args or {}),
                        )
                        yield StreamChunk(type="tool_call", tool_call=tc)
        yield StreamChunk(type="done")

    def count_tokens(self, text: str) -> int:
        """调用官方 count_tokens，失败则按字符粗估。"""
        try:
            resp = self._client.models.count_tokens(
                model=self._model, contents=text
            )
            return getattr(resp, "total_tokens", 0) or 0
        except Exception:
            return len(text) // 4
