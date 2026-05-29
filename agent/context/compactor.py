# -*- coding: utf-8 -*-
"""增量压缩策略：用 LLM 总结旧消息，保留近期完整上下文。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..config import CFG
from ..llm.base import LLMProvider, Message
from ..utils.log import log, wait_run
from .window import ContextWindow


class Compactor:
    """对话压缩器。"""

    def __init__(self, provider: LLMProvider):
        self._provider = provider

    def _write_transcript(self, messages: list[Message]) -> Path:
        """写入 transcript 备份。"""
        CFG.transcript_dir.mkdir(parents=True, exist_ok=True)
        path = CFG.transcript_dir / f"transcript_{int(time.time())}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps({
                    "role": msg.role, "content": msg.content,
                    "tool_calls": [{"id": tc.id, "name": tc.name, "args": tc.arguments} for tc in (msg.tool_calls or [])],
                }, ensure_ascii=False, default=str) + "\n")
        return path

    def _summarize(self, messages: list[Message]) -> str:
        """用 LLM 生成对话摘要。"""
        conv = json.dumps(
            [{"role": m.role, "content": m.content[:2000]} for m in messages],
            ensure_ascii=False,
        )[:80000]
        prompt = (
            "请总结这段编程代理对话，保留：\n"
            "1. 当前目标\n2. 关键发现与决策\n3. 已修改的文件\n"
            "4. 剩余待办\n5. 用户约束\n\n"
            f"{conv}"
        )
        resp = wait_run(
            "总结",
            lambda: self._provider.chat([Message.user(prompt)]),
        )
        return resp.content.strip()

    def compact(self, window: ContextWindow, *, focus: str | None = None) -> list[Message]:
        """执行压缩：写 transcript，生成摘要，替换历史。"""
        tp = self._write_transcript(window.messages)
        log("INFO", "transcript_saved", str(tp))

        summary = self._summarize(window.messages)
        if focus:
            summary += f"\n\n下一步重点：{focus}"
        if window.recent_files:
            files = "\n".join(f"- {p}" for p in window.recent_files)
            summary += f"\n\n近期文件（可优先重开）：\n{files}"

        window.has_compacted = True
        window.last_summary = summary

        return [Message.user(f"对话已压缩，请据此继续。\n\n{summary}")]
