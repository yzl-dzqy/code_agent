# -*- coding: utf-8 -*-
"""
终端 REPL 界面。

职责：
  - 读取用户输入 → 调用 Agent.chat()
  - 斜杠命令（/todo、/bg、/cron 等）本地处理
  - 通过 AgentCallbacks 展示工具调用和回复
"""

from __future__ import annotations

import json
import re

from ..agent import AgentCallbacks, export_conversation
from ..background import BG_MGR
from ..bootstrap import create_agent, init_runtime
from ..mcp.integration import shutdown_mcp
from ..config import CFG
from ..memory.long_term import MEMORY_MGR
from ..planner import PLANNER
from ..scheduler import SCHEDULER
from ..skills import SKILLS
from ..tasks import TASK_MGR
from ..worktree import WT_MGR
from ..mcp.router import MCP_ROUTER
from ..utils.log import log, log_list, preview_text
from ..utils.metrics import METRICS


# ── 待办看板渲染 ──

def _print_todo_board():
    """在终端打印彩色待办看板。"""
    items = PLANNER.state.items
    if not items:
        return
    R, dim, bold = "\033[0m", "\033[2m", "\033[1m"
    cy, yl, gr = "\033[36m", "\033[33m", "\033[32m"
    bar, pct = PLANNER.progress_bar()
    c, ip, pe, total = PLANNER.counts()
    print(f"\n{cy}{'─'*44}{R}")
    print(f"{bold}{cy} 待办看板{R}")
    print(f"  {bold}进度{R}  {gr}{c}{R}/{total}  [{yl}{bar}{R}] {bold}{pct}%{R}")
    for i, it in enumerate(items, 1):
        if it.status == "completed":
            sym, color = "[✓]", gr
        elif it.status == "in_progress":
            sym, color = "[→]", yl
        else:
            sym, color = "[ ]", dim
        line = f"  {color}{sym}{R} {i:2}. {it.content}"
        if it.status == "in_progress" and it.active_form:
            line += f" {dim}({it.active_form}){R}"
        print(line)
    print(f"{cy}{'─'*44}{R}")


# ── 斜杠命令分发 ──

def _handle_slash(cmd: str, text: str, agent) -> bool:
    """
    处理斜杠命令，返回 True 表示已处理（调用方应 continue）。

    将所有 /xxx 命令集中到此函数，CLI 主循环只负责读取输入和调用 agent。
    """
    if cmd in ("/q", "exit", "quit"):
        return False  # 特殊：返回 False 表示退出

    handlers = {
        "/help":     lambda: print(HELP_TEXT),
        "/stats":    lambda: print(f"\033[36m[指标]\033[0m {METRICS.summary_line()}"),
        "/log":      lambda: print("\n".join(log_list)),
        "/todo":     _print_todo_board,
        "/bg":       lambda: print(f"\033[36m[后台任务]\033[0m\n{BG_MGR.check()}"),
        "/cron":     lambda: print(f"\033[36m[定时任务]\033[0m\n{SCHEDULER.list_tasks()}"),
        "/tasks":    lambda: print(f"\033[36m[持久任务]\033[0m\n{TASK_MGR.list_all()}"),
        "/worktree": lambda: print(f"\033[36m[Worktree]\033[0m\n{WT_MGR.list_all()}"),
        "/export":   lambda: print(f"\033[36m已导出:\033[0m {export_conversation(agent.get_history())}"),
        "/prompt":   lambda: print(f"--- System Prompt ---\n{agent.prompt_builder.build()}\n--- End ---"),
        "/sections": lambda: _show_sections(agent),
        "/health":   lambda: _show_health(agent),
        "/memories": _show_memories,
        "/hooks":    lambda: print(f"\033[36m[Hooks]\033[0m {agent.hooks.summary()}"),
        "/skills":   _show_skills,
        "/mcp":      _show_mcp,
    }

    if cmd in handlers:
        handlers[cmd]()
        return True

    # /model [name] 需要参数解析
    if cmd == "/model":
        parts = text.split()
        if len(parts) == 1:
            current = f"{agent.provider_name}:{agent.provider.model_name}"
            print(f"\033[36m当前模型:\033[0m {current}")
            for m in CFG.known_models:
                mark = " ◀" if m == current else ""
                print(f"  {m}{mark}")
            print("用法: /model <provider:model> 或 /model <provider> <model>")
        else:
            try:
                if len(parts) >= 3:
                    old, new = agent.switch_model(parts[2], provider_name=parts[1])
                else:
                    old, new = agent.switch_model(parts[1])
            except Exception as exc:
                print(f"\033[31m切换失败:\033[0m {exc}")
            else:
                print(f"\033[36m已切换:\033[0m {old.ref} → \033[1m{new.ref}\033[0m")
        return True

    return True  # 未知斜杠命令，静默忽略


def _show_sections(agent):
    headers = agent.prompt_builder.section_headers()
    print("\033[36m[Prompt Sections]\033[0m")
    for h in headers:
        print(f"  {h}")


def _show_health(agent):
    key_map = {
        "gemini": CFG.gemini_api_key,
        "openai": CFG.openai_api_key,
        "claude": CFG.anthropic_api_key,
        "ollama": "local",
    }
    key_status = "已配置" if key_map.get(agent.provider_name) else "未配置"
    print(f"\033[36m[健康]\033[0m provider={agent.provider_name} "
          f"model={agent.provider.model_name} key={key_status}")


def _show_memories():
    entries = MEMORY_MGR.list_all()
    if entries:
        print("\033[36m[持久记忆]\033[0m")
        for e in entries:
            print(f"  [{e.mem_type}] {e.name}: {e.description}")
    else:
        print("\033[33m暂无记忆。Agent 可通过 save_memory 工具创建。\033[0m")


def _show_mcp():
    """已连接的 MCP 服务及其工具数量。"""
    if not MCP_ROUTER.clients:
        print("\033[36m[MCP]\033[0m 未连接（可在工作目录放置 .claude-plugin/plugin.json）")
        return
    print("\033[36m[MCP]\033[0m 已连接服务:")
    for name, client in MCP_ROUTER.clients.items():
        n = len(client._tools)
        print(f"  • {name}  ({n} tools)")


def _show_skills():
    available = SKILLS.list_skills()
    if available:
        print("\033[36m[可用 Skills]\033[0m")
        for s in available:
            print(f"  /{s.name}  — {s.description}")
        print("用法: 在输入中加 /skill-name 自动加载 skill 上下文")
    else:
        print("\033[33m暂无可用 Skill\033[0m")


# ── 提示文本 ──

HINT = "输入 /help 查看命令 | Ctrl-C 退出"

HELP_TEXT = """\
\033[1m\033[36m可用命令:\033[0m
  \033[33m/help\033[0m       显示此帮助信息
  \033[33m/q\033[0m          退出
  \033[33m/model\033[0m      查看/切换模型 (/model <name>)
  \033[33m/todo\033[0m       显示待办看板
  \033[33m/tasks\033[0m      显示持久任务
  \033[33m/bg\033[0m         显示后台任务
  \033[33m/cron\033[0m       显示定时任务
  \033[33m/worktree\033[0m   显示 Worktree 隔离通道
  \033[33m/memories\033[0m   显示持久记忆
  \033[33m/hooks\033[0m      显示激活的 Hook
  \033[33m/skills\033[0m     显示可用 Skills (/skill-name 内联加载)
  \033[33m/mcp\033[0m        显示已连接的 MCP 服务
  \033[33m/stats\033[0m      显示会话指标
  \033[33m/log\033[0m        显示最近日志
  \033[33m/health\033[0m     显示健康状态
  \033[33m/prompt\033[0m     显示当前系统提示词
  \033[33m/sections\033[0m   显示提示词各段标题
  \033[33m/export\033[0m     导出对话历史"""


# ── 主入口 ──

def main(initial_query: str = ""):
    """CLI REPL 主循环或单次执行。"""
    init_runtime(enable_wait_indicator=CFG.wait_indicator)

    import sys
    import os
    is_tty = sys.stdout.isatty()
    is_raw_mode = os.environ.get("AGENT_RAW_MODE") == "1"

    def _sanitize_raw_output(text: str) -> str:
        """
        Raw 模式兜底清洗，避免重定向文件混入思考和 Markdown 包裹。
        """
        out = text or ""
        # 中文注释：移除 <thinking>...</thinking>，避免污染代码文件。
        out = re.sub(r"(?is)<thinking>.*?</thinking>", "", out)
        # 移除 ```lang ... ``` 或 ``` ... ``` 包裹，仅保留主体内容。
        out = re.sub(r"(?is)^\s*```[a-zA-Z0-9_-]*\s*\n", "", out)
        out = re.sub(r"(?is)\n```+\s*$", "", out)
        return out.strip()

    def _print_text(t: str) -> None:
        content = _sanitize_raw_output(t) if is_raw_mode else t
        if is_tty and not is_raw_mode:
            print(f"\033[32m{content}\033[0m")
        else:
            print(content)

    def _print_tool_start(n: str, a: dict) -> None:
        msg = f"  ⚙ {n} {preview_text(json.dumps(a, ensure_ascii=False), 80)}"
        if is_tty:
            print(f"\n\033[33m{msg}\033[0m")
        else:
            print(msg, file=sys.stderr)

    callbacks = AgentCallbacks(
        on_text=_print_text,
        on_tool_start=_print_tool_start,
        on_tool_end=lambda n, r: None,
        on_todo_update=_print_todo_board if is_tty else None,
        on_system=lambda msg: print(f"\033[36m{msg}\033[0m") if is_tty else print(msg, file=sys.stderr)
    )
    provider, registry, agent = create_agent(callbacks)

    if initial_query:
        # 单次执行模式 (One-shot)
        try:
            METRICS.user_messages += 1
            agent.chat(initial_query)
            if is_tty and not is_raw_mode:
                print()
        except (KeyboardInterrupt, BrokenPipeError):
            pass
        finally:
            shutdown_mcp()
        return

    print(HINT)

    while True:
        try:
            try:
                query = input("\033[36m>> \033[0m")
            except (EOFError, KeyboardInterrupt):
                break

            text = query.strip()
            if not text:
                continue

            low = text.lower()

            # 退出
            if low in ("/q", "exit", "quit"):
                break

            # 斜杠命令
            if low.startswith("/"):
                cmd = low.split()[0]
                _handle_slash(cmd, text, agent)
                continue

            # 正常对话
            METRICS.user_messages += 1
            agent.chat(text)
            print()
        except (KeyboardInterrupt, BrokenPipeError):
            print("\n[已中断]")
            break

    shutdown_mcp()
