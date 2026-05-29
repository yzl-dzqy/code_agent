# -*- coding: utf-8 -*-
"""用户交互工具：ask_user。"""

from __future__ import annotations

from ...utils.log import log, preview_text
from ..registry import tool

# UI 层通过注入此回调实现 TUI 弹窗
_ask_user_hook: callable | None = None


def set_ask_user_hook(fn):
    global _ask_user_hook
    _ask_user_hook = fn


@tool(name="ask_user", description="向用户提问并等待回答", parallel=False)
def ask_user(question: str, default: str = "") -> str:
    if _ask_user_hook is not None:
        log("INFO", "ask_user_tui", preview_text(question, 120))
        return _ask_user_hook(question, default)
    log("INFO", "ask_user", preview_text(question, 120))
    print(f"\n\033[33m[追问]\033[0m {question}")
    if default:
        print(f"\033[90m(回车使用默认: {default})\033[0m")
    try:
        line = input("\033[36m你的回复 >> \033[0m").strip()
    except (EOFError, KeyboardInterrupt):
        return default or "(用户取消)"
    return line if line else (default or "(空回复)")
