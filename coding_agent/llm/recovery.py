# -*- coding: utf-8 -*-
"""LLM call recovery policy used by the agent runner."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .base import LLMProvider, Message, ToolSpec
from ..utils.log import log, wait_run
from ..utils.metrics import METRICS

if TYPE_CHECKING:
    from ..context.compactor import Compactor
    from ..context.window import ContextWindow


EmitSystem = Callable[[str], None]
BuildSystem = Callable[[], str]


@dataclass
class LLMRecoveryPolicy:
    """Centralizes model call retry and response repair behavior."""

    max_attempts: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    continuation_message: str = (
        "输出长度已达上限，请直接从上次停止的位置继续——"
        "不要复述、不要重复，必要时可从半句中间接续。"
    )

    def call(
        self,
        provider: LLMProvider,
        window: ContextWindow,
        compactor: Compactor,
        *,
        tools: list[ToolSpec],
        system: str,
        build_system: BuildSystem,
        emit_system: EmitSystem,
    ) -> Message:
        """
        调用 LLM，集成三条恢复路径：

          1. malformed response → 注入修复提示后重试
          2. prompt 过长 → 自动 compact 后重试
          3. 网络/限流 → 指数退避后重试
        """
        response: Message | None = None

        for attempt in range(self.max_attempts + 1):
            try:
                response = wait_run(
                    "推理",
                    lambda: provider.chat(window.messages, tools=tools, system=system),
                )
                break
            except Exception as exc:
                err = str(exc).lower()

                if self._is_context_error(err):
                    log("WARN", "recovery_compact", f"attempt={attempt + 1}")
                    emit_system(f"[恢复] 上下文过长，自动压缩… (第 {attempt + 1} 次)")
                    window.messages = compactor.compact(window)
                    system = build_system()
                    continue

                if attempt < self.max_attempts:
                    delay = self._backoff_delay(attempt)
                    log(
                        "WARN",
                        "recovery_backoff",
                        f"attempt={attempt + 1} delay={delay:.1f}s error={str(exc)[:120]}",
                    )
                    emit_system(
                        f"[恢复] API 错误，{delay:.1f}s 后重试 "
                        f"({attempt + 1}/{self.max_attempts})"
                    )
                    time.sleep(delay)
                    continue

                log("ERROR", "recovery_exhausted", str(exc)[:200])
                return Message.assistant(f"（API 调用失败：{str(exc)[:300]}）")

        if response is None:
            return Message.assistant("（未收到模型响应）")

        return self._repair_malformed_response(
            provider,
            window,
            tools=tools,
            system=system,
            response=response,
        )

    def needs_continuation(self, response: Message) -> bool:
        """判断响应是否因 max_tokens 被截断，需要续写。"""
        finish = getattr(response, "finish_reason", "") or ""
        if finish == "max_tokens":
            return True
        if (
            not response.tool_calls
            and response.content
            and len(response.content) > 7000
            and not response.content.rstrip().endswith(("。", ".", "！", "!", "\n"))
        ):
            return True
        return False

    def _repair_malformed_response(
        self,
        provider: LLMProvider,
        window: ContextWindow,
        *,
        tools: list[ToolSpec],
        system: str,
        response: Message,
    ) -> Message:
        for repair in range(self.max_attempts):
            if response.content or response.tool_calls:
                break
            log("WARN", "malformed_response", f"repair={repair + 1}/{self.max_attempts}")
            hint = (
                "[系统] 上一次响应为空或格式无效，请重新生成。"
                "确保工具调用参数为合法 JSON，字段类型与 schema 一致。"
            ) if repair < self.max_attempts - 1 else (
                "[系统] 多次响应失败，请直接用自然语言回答。"
            )
            window.add(Message.user(hint))
            try:
                response = wait_run(
                    "修复",
                    lambda: provider.chat(window.messages, tools=tools, system=system),
                )
            except Exception:
                break
            METRICS.record_model_turn()
        return response

    def _backoff_delay(self, attempt: int) -> float:
        delay = min(self.backoff_base * (2 ** attempt), self.backoff_max)
        return delay + random.uniform(0, 1)

    @staticmethod
    def _is_context_error(error_text: str) -> bool:
        return any(
            marker in error_text
            for marker in (
                "too long",
                "overlong",
                "token limit",
                "context_length",
                "prompt_too_long",
                "request payload size",
            )
        )
