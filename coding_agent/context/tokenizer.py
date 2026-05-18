# -*- coding: utf-8 -*-
"""统一 token 计数：优先用 tiktoken，回退到字符估算。"""

from __future__ import annotations

_enc = None


def _get_encoder():
    global _enc
    if _enc is None:
        try:
            import tiktoken
            _enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _enc = False  # 标记不可用
    return _enc


def count_tokens(text: str) -> int:
    """估算文本 token 数。"""
    enc = _get_encoder()
    if enc:
        return len(enc.encode(text))
    # 回退：中英混合约 2 字符/token
    return max(1, len(text) // 2)


def count_messages_tokens(messages: list) -> int:
    """估算消息列表的总 token 数。"""
    total = 0
    for msg in messages:
        content = getattr(msg, "content", "") or ""
        total += count_tokens(content) + 4  # 消息元数据开销
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                total += count_tokens(str(tc.arguments)) + 10
    return total
