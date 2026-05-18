# -*- coding: utf-8 -*-
"""日志、等待动画、预览文本等基础工具。"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

_T = TypeVar("_T")

# 运行时赋值（避免循环导入）
_log_dir = None
_log_file = None
_log_enabled = True
_wait_indicator = True
_session_id = ""

# 最近日志（供 CLI 查看）
log_list: list[str] = []


def init_log(log_dir, log_file, enabled: bool, wait_ind: bool, session_id: str) -> None:
    """由启动入口调用，注入日志目录、文件、开关与会话 ID。"""
    global _log_dir, _log_file, _log_enabled, _wait_indicator, _session_id
    _log_dir = log_dir
    _log_file = log_file
    _log_enabled = enabled
    _wait_indicator = wait_ind
    _session_id = session_id


def log(level: str, event: str, detail: str = "") -> None:
    """写入一行结构化日志，并保留最近若干行于内存供 CLI 查看。"""
    if not _log_enabled or _log_file is None:
        return
    now = datetime.now().strftime("%H:%M:%S")
    rid = f"{_session_id[:8]}|" if _session_id else ""
    line = f"[{now}] [{rid}{level}] {event}"
    if detail:
        line += f" | {detail}"
    _log_dir.mkdir(parents=True, exist_ok=True)
    with _log_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    log_list.append(line)
    if len(log_list) > 20:
        log_list.pop(0)


def _in_tui() -> bool:
    return os.getenv("AGENT_IN_TUI", "").strip() == "1"


def wait_run(label: str, fn: Callable[[], _T]) -> _T:
    """阻塞执行 fn；非 TUI 且 stderr 为 TTY 时在终端显示等待动画。"""
    if _in_tui() or not _wait_indicator or not sys.stderr.isatty():
        return fn()
    stop = threading.Event()
    frames = "|/-\\"

    def spin():
        i = 0
        while not stop.wait(0.12):
            sys.stderr.write(f"\r\033[33m[{label}] {frames[i % len(frames)]}\033[0m 等待…")
            sys.stderr.flush()
            i += 1
        sys.stderr.write("\r" + " " * 50 + "\r")
        sys.stderr.flush()

    t = threading.Thread(target=spin, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop.set()
        t.join(timeout=3)


def preview_text(text: str, limit: int = 300) -> str:
    """将文本压成单行并截断，用于日志与 UI 摘要。"""
    safe = (text or "").replace("\n", " ")
    return safe if len(safe) <= limit else f"{safe[:limit]}...(+{len(safe)-limit})"
