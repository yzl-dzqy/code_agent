# -*- coding: utf-8 -*-
"""Session checkpoint persistence for ContextWindow messages."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from ..llm.base import Message, ToolCall


class SessionCheckpoint:
    """Persist and restore short-term conversation state."""

    def __init__(self, path: Path, *, enabled: bool = True):
        self.path = path
        self.enabled = enabled

    def save(self, messages: list[Message]) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "messages": [self._message_to_dict(m) for m in messages],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> list[Message]:
        if not self.enabled or not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            return []
        raw_messages = payload.get("messages", [])
        if not isinstance(raw_messages, list):
            return []
        return [self._message_from_dict(m) for m in raw_messages if isinstance(m, dict)]

    @staticmethod
    def _message_to_dict(message: Message) -> dict:
        data = asdict(message)
        data["tool_calls"] = [asdict(tc) for tc in (message.tool_calls or [])]
        return data

    @staticmethod
    def _message_from_dict(data: dict) -> Message:
        tool_calls = data.get("tool_calls")
        parsed_calls = None
        if isinstance(tool_calls, list):
            parsed_calls = [
                ToolCall(
                    id=str(tc.get("id", "")),
                    name=str(tc.get("name", "")),
                    arguments=dict(tc.get("arguments", {})),
                )
                for tc in tool_calls
                if isinstance(tc, dict)
            ]
        return Message(
            role=data.get("role", "user"),
            content=str(data.get("content", "")),
            tool_calls=parsed_calls,
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            finish_reason=str(data.get("finish_reason", "")),
        )
