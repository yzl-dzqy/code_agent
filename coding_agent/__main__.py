# -*- coding: utf-8 -*-
"""python -m coding_agent [--tui] 入口。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _import_tui_runner():
    """兼容 `python -m coding_agent` 与 `python coding_agent` 两种入口。"""
    try:
        from .ui.tui import run_tui
        return run_tui
    except ImportError:
        from coding_agent.ui.tui import run_tui
        return run_tui


def _import_cli_main():
    """兼容 `python -m coding_agent` 与 `python coding_agent` 两种入口。"""
    try:
        from .ui.cli import main as cli_main
        return cli_main
    except ImportError:
        from coding_agent.ui.cli import main as cli_main
        return cli_main


def _ensure_import_path() -> None:
    """
    兼容 `python coding_agent` 直接运行目录的场景：
    将包上级目录加入 sys.path，使 `import coding_agent.*` 可用。
    """
    pkg_dir = Path(__file__).resolve().parent
    project_root = str(pkg_dir.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


def main():
    _ensure_import_path()
    parser = argparse.ArgumentParser(description="Coding Agent")
    parser.add_argument("--cli", action="store_true", help="强制使用旧版纯文本 CLI 模式")
    parser.add_argument("--provider", type=str, help="LLM provider: gemini/openai/claude/ollama")
    parser.add_argument("--model", type=str, help="模型名称")
    parser.add_argument("-p", "--prompt", type=str, help="作为一次性任务执行并退出（不进入交互模式）")
    parser.add_argument("-r", "--raw", action="store_true", help="强制进入 Raw 模式（重定向时会自动开启），输出内容去除 Markdown 标记和闲聊")
    args, _ = parser.parse_known_args()

    # 环境变量覆盖
    if args.provider:
        os.environ["AGENT_PROVIDER"] = args.provider
    if args.model:
        os.environ["AGENT_MODEL"] = args.model

    import select
    
    is_piped = not sys.stdout.isatty()
    is_raw_mode = args.raw or is_piped
    if is_raw_mode:
        os.environ["AGENT_RAW_MODE"] = "1"
        use_tui = False
    
    stdin_content = ""
    # Use select to check if stdin has data, avoiding blocking if it's just not a tty but empty
    if not sys.stdin.isatty():
        rlist, _, _ = select.select([sys.stdin], [], [], 0.0)
        if rlist:
            stdin_content = sys.stdin.read().strip()

    initial_query = ""
    if args.prompt or stdin_content:
        parts = []
        if args.prompt:
            parts.append(args.prompt)
        if stdin_content:
            parts.append(f"以下是附带的内容：\n\n{stdin_content}")
        initial_query = "\n\n".join(parts)

    # 默认情况下，如果是交互式 TTY 且未请求一次性执行、且未强制 --cli，则进入 TUI。
    use_tui = sys.stdout.isatty() and not initial_query and not args.cli

    if use_tui:
        os.environ["AGENT_IN_TUI"] = "1"
        try:
            run_tui = _import_tui_runner()
        except ImportError as exc:
            print(f"提示: 未安装 textual 或发生错误，回退到普通 CLI。({exc})")
            cli_main = _import_cli_main()
            cli_main(initial_query)
        else:
            run_tui()
    else:
        cli_main = _import_cli_main()
        cli_main(initial_query)


if __name__ == "__main__":
    main()
