# -*- coding: utf-8 -*-


from __future__ import annotations

import json
import re
import subprocess
import threading
from datetime import datetime

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.worker import get_current_worker
from textual.widgets import (
    Button, Input, Label, ListItem,
    ListView, LoadingIndicator, RichLog, Static,
)

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
from ..tools.builtin.user import set_ask_user_hook
from ..utils.log import log, preview_text
from ..utils.metrics import METRICS

SLASH_COMMANDS = [
    "/model",
    "/workdir",
    "/hooks",
    "/stop",
    "/help",
    "/q",
    "/todo",
    "/tasks",
    "/bg",
    "/cron",
    "/worktree",
    "/memories",
    "/skills",
    "/prompt",
    "/sections",
    "/export",
    "/clear",
    "/clear_todo",
    "/metrics",
]


# ═══════════════════════════════════════════════════════════════
# 1. 辅助函数
# ═══════════════════════════════════════════════════════════════

def _notify(title: str, message: str):
    """macOS 桌面通知（静默失败）。"""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


def _todo_rich() -> str:
    """生成待办看板的 Rich 标记文本。"""
    items = PLANNER.state.items
    if not items:
        return ""
    lines: list[str] = []
    for i, it in enumerate(items, 1):
        if it.status == "completed":
            sym, text = "[green]✓[/]", f"[dim]{it.content}[/]"
        elif it.status == "in_progress":
            sym = "[yellow bold]→[/]"
            text = f"[bold]{it.content}[/]"
            if it.active_form:
                text += f" [dim]({it.active_form})[/]"
        else:
            sym, text = "[dim]·[/]", it.content
        lines.append(f" {sym} {i:2}. {text}")
    c, _, _, total = PLANNER.counts()
    pct = int(100 * c / total) if total else 0
    filled = round(20 * c / total) if total else 0
    bar = "█" * filled + "░" * (20 - filled)
    lines += ["", f"  [green]{bar}[/] [bold]{pct}%[/]  {c}/{total}\n"]
    return "\n".join(lines)


def _metrics_rich() -> str:
    """生成指标面板的 Rich 标记文本。"""
    return (
        f"\n[bold]Metrics[/]\n"
        f"  [dim]Model Turns[/]  [bold]{METRICS.model_turns}[/]\n"
        f"  [dim]Tool Calls[/]  [bold]{METRICS.tool_invocations}[/]\n"
        f"  [dim]User Msgs[/]  [bold]{METRICS.user_messages}[/]\n"
        f"  [dim]Tool Time[/]  [bold]{METRICS.tool_wall_ms:.0f} ms[/]\n"
        f"  [dim]Tokens In[/]  [bold]{METRICS.prompt_tokens}[/]\n"
        f"  [dim]Tokens Out[/]  [bold]{METRICS.output_tokens}[/]\n"
    )


# ═══════════════════════════════════════════════════════════════
# 2. 输入框组件（支持 ↑↓ 历史翻阅）
# ═══════════════════════════════════════════════════════════════

class _HistoryInput(Input):
    """带历史记录翻阅的输入框。"""

    def __init__(self, sent: list[str], slash_commands: list[str] | None = None, **kw):
        super().__init__(**kw)
        self._sent = sent
        self._idx = len(sent)
        self._slash_commands = [c.lower() for c in (slash_commands or [])]
        self._completion_matches: list[str] = []
        self._completion_index: int = -1

    def on_key(self, event):
        if event.key == "up":
            # 中文注释：若当前是 / 命令输入，则优先在候选命令中循环选择。
            if self._navigate_completion(reverse=True):
                event.prevent_default()
                return
            if self._sent and self._idx > 0:
                self._idx -= 1
                self.value = self._sent[self._idx]
                self.cursor_position = len(self.value)
                event.prevent_default()
        elif event.key == "down":
            if self._navigate_completion(reverse=False):
                event.prevent_default()
                return
            if self._idx < len(self._sent) - 1:
                self._idx += 1
                self.value = self._sent[self._idx]
                self.cursor_position = len(self.value)
            elif self._idx == len(self._sent) - 1:
                self._idx = len(self._sent)
                self.value = ""
            event.prevent_default()
        elif event.key == "tab":
            if self._autocomplete_slash():
                event.prevent_default()

    def push(self, text: str):
        self._sent.append(text)
        self._idx = len(self._sent)

    def _autocomplete_slash(self) -> bool:
        """
        Tab 自动补全斜杠命令。
        - 唯一匹配：直接补全为完整命令并追加空格
        - 多个匹配：补到公共前缀
        """
        raw = self.value or ""
        prefix = raw.strip().lower()
        if not prefix.startswith("/") or " " in prefix:
            self._reset_completion_state()
            return False
        matches = self._matching_commands(prefix)
        if not matches:
            self._reset_completion_state()
            return False
        self._completion_matches = matches
        self._completion_index = 0
        if len(matches) == 1:
            self.value = matches[0] + " "
            self.cursor_position = len(self.value)
            return True
        common = matches[0]
        for cmd in matches[1:]:
            i = 0
            max_len = min(len(common), len(cmd))
            while i < max_len and common[i] == cmd[i]:
                i += 1
            common = common[:i]
            if not common:
                break
        if len(common) > len(prefix):
            self.value = common
            self.cursor_position = len(self.value)
            return True
        return False

    def _matching_commands(self, prefix: str) -> list[str]:
        """返回按前缀匹配到的命令列表。"""
        return [cmd for cmd in self._slash_commands if cmd.startswith(prefix)]

    def resolve_single_slash(self, raw_text: str) -> str | None:
        """
        若输入是 / 前缀且仅匹配一个命令，返回该命令；否则返回 None。
        用于 Enter 自动确认。
        """
        prefix = (raw_text or "").strip().lower()
        if not prefix.startswith("/") or " " in prefix:
            return None
        matches = self._matching_commands(prefix)
        return matches[0] if len(matches) == 1 else None

    def _reset_completion_state(self) -> None:
        self._completion_matches = []
        self._completion_index = -1

    def _navigate_completion(self, reverse: bool = False) -> bool:
        """用上下键在 / 命令候选中循环选择。"""
        raw = self.value or ""
        prefix = raw.strip().lower()
        if not prefix.startswith("/") or " " in prefix:
            self._reset_completion_state()
            return False
        matches = self._matching_commands(prefix)
        if not matches:
            self._reset_completion_state()
            return False
        if matches != self._completion_matches:
            self._completion_matches = matches
            self._completion_index = 0
        else:
            step = -1 if reverse else 1
            self._completion_index = (self._completion_index + step) % len(matches)
        self.value = self._completion_matches[self._completion_index]
        self.cursor_position = len(self.value)
        return True


class ModelSwitchModal(ModalScreen[str | None]):
    """模型切换弹窗：列表选择 + 自定义输入。"""
    DEFAULT_CSS = """
    ModelSwitchModal { align: center middle; }
    #model_dialog { width: 60; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #model_list { height: auto; max-height: 14; margin-bottom: 1; }
    #custom_input { width: 100%; margin-bottom: 1; }
    #btn_row { layout: horizontal; height: 3; }
    #btn_ok { width: 1fr; margin-right: 1; }
    #btn_cancel { width: 1fr; }
    """
    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, current: str):
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="model_dialog"):
            yield Static(
                f"[bold]切换模型[/]  当前: [yellow]{self._current}[/]",
                markup=True)
            yield ListView(
                *[ListItem(
                    Label(f"{'▶ ' if m == self._current else '  '}{m}"),
                    id=f"m_{i}")
                  for i, m in enumerate(CFG.known_models)],
                id="model_list",
            )
            yield Static("[dim]或输入自定义模型：[/]", markup=True)
            yield Input(id="custom_input", placeholder="如 gemini-2.5-pro")
            with Horizontal(id="btn_row"):
                yield Button("确认", id="btn_ok", variant="primary")
                yield Button("取消", id="btn_cancel")

    def on_list_view_selected(self, ev: ListView.Selected):
        idx = int(ev.item.id.split("_")[1])
        self.dismiss(CFG.known_models[idx])

    def on_button_pressed(self, ev: Button.Pressed):
        if ev.button.id == "btn_ok":
            custom = self.query_one("#custom_input", Input).value.strip()
            if custom:
                self.dismiss(custom)
            else:
                lv = self.query_one("#model_list", ListView)
                self.dismiss(
                    CFG.known_models[lv.index] if lv.index is not None else None)
        else:
            self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)


# ═══════════════════════════════════════════════════════════════
# 4. 主应用
# ═══════════════════════════════════════════════════════════════

class AgentTUIApp(App[None]):
    """Textual TUI 主应用。"""

    CSS = """
    Screen { background: $background; }
    #main_row { height: 1fr; }
    
    #chat_panel { 
        width: 1fr; 
        layout: vertical; 
        border: none;
    }
    #chat { height: 1fr; padding: 0 2; }
    
    #status_row { 
        height: 1; 
        layout: horizontal; 
        padding: 0 2;
        color: $text-muted;
    }
    
    #input_panel { 
        height: auto; 
        border-top: solid $primary;
        padding: 1 2;
        background: $surface;
    }
    #prompt { 
        width: 1fr; 
        border: none;
        background: $surface;
    }
    
    #loading_container {
        width: 100%;
        height: auto;
        padding: 0 2;
        display: none;
    }
    #loading_container.visible {
        display: block;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", show=True),
        Binding("ctrl+c", "interrupt_generation", "中断生成", show=True),
        Binding("ctrl+m", "switch_model", "切换模型", show=True),
        Binding("ctrl+e", "export_chat", "导出", show=True),
        Binding("ctrl+l", "clear_chat", "清屏", show=True),
    ]

    busy: reactive[bool] = reactive(False)

    def __init__(self):
        super().__init__()
        self._sent_history: list[str] = []
        self._agent = None
        self._provider = None
        # ask_user 内联等待机制
        self._ask_event: threading.Event | None = None
        self._ask_result: list[str] = []
        self._ask_default: str = ""
        self._original_placeholder: str = ""
        self._active_worker = None
        self._interrupt_requested = False

    # ── 布局 ──

    def compose(self) -> ComposeResult:
        with Vertical(id="main_row"):
            with Vertical(id="chat_panel"):
                yield RichLog(id="chat", highlight=True, markup=True, auto_scroll=True)
            
            with Horizontal(id="status_row"):
                yield Label("✓ Ready", id="status_label")
                yield Label("", id="clock_label")
            
            with Vertical(id="loading_container"):
                yield Label("⏳ [yellow]Thinking...[/]", id="loading_text")

            with Vertical(id="input_panel"):
                yield _HistoryInput(
                    self._sent_history, slash_commands=SLASH_COMMANDS, id="prompt",
                    placeholder="Ask a question or enter a command... (↑↓ history, Ctrl+M model, Ctrl+Q quit)")

    # ── 初始化 ──

    def on_mount(self):
        """启动时初始化：使用 bootstrap 统一创建 Agent。"""
        init_runtime(enable_wait_indicator=False)

        callbacks = AgentCallbacks(
            on_tool_start=lambda n, a: self.call_from_thread(
                lambda: self._chat_write(
                    f"  [yellow dim]⚙ {n}[/] "
                    f"[dim]{preview_text(json.dumps(a, ensure_ascii=False), 60)}[/]")),
            on_tool_end=lambda n, r: None,
            on_todo_update=lambda: self.call_from_thread(self._refresh_todo),
        )

        self._provider, _, self._agent = create_agent(
            callbacks, ask_user_hook=self._hook_ask_user)

        self._refresh_todo()
        self._update_title()
        self.query_one("#prompt", _HistoryInput).focus()
        self.set_interval(1, self._tick_clock)

    def on_unmount(self):
        set_ask_user_hook(None)
        shutdown_mcp()

    # ── 状态响应 ──

    def watch_busy(self, value: bool):
        prompt = self.query_one("#prompt", _HistoryInput)
        prompt.disabled = value
        loading_container = self.query_one("#loading_container")
        if value:
            loading_container.add_class("visible")
        else:
            loading_container.remove_class("visible")
            prompt.focus()
        
        self.query_one("#status_label", Label).update(
            "[yellow]Working...[/]" if value else "[green]Ready[/]")

    # ── UI 辅助 ──

    def _chat_write(self, text: str):
        self.query_one("#chat", RichLog).write(text)

    def _chat_write_markdown(self, text: str):
        """将文本按 Markdown 渲染后写入聊天区。"""
        md = Markdown(text or "")
        self.query_one("#chat", RichLog).write(md)

    def _write_assistant_content(self, text: str):
        """
        输出助手内容：
        - <thinking>...</thinking> 标签内内容使用绿色
        - 其他内容沿用 Markdown/纯文本策略
        """
        content = text or ""
        pattern = re.compile(r"(?is)<thinking>(.*?)</thinking>")
        pos = 0
        chat = self.query_one("#chat", RichLog)

        for match in pattern.finditer(content):
            before = content[pos:match.start()]
            if before:
                if self._looks_like_markdown(before):
                    self._chat_write_markdown(before)
                else:
                    self._chat_write(before)

            thinking_body = match.group(1)
            self._chat_write("[dim]thinking:" + thinking_body)
            pos = match.end()

        tail = content[pos:]
        if tail:
            if self._looks_like_markdown(tail):
                self._chat_write_markdown(tail)
            else:
                self._chat_write(tail)

    @staticmethod
    def _looks_like_markdown(text: str) -> bool:
        """简单判断文本是否包含常见 Markdown 语法。"""
        if not text:
            return False
        patterns = (
            r"(?m)^\s{0,3}#{1,6}\s+\S",            # 标题
            r"(?m)^\s*[-*+]\s+\S",                 # 无序列表
            r"(?m)^\s*\d+\.\s+\S",                 # 有序列表
            r"(?m)^```",                           # 代码块
            r"(?m)^\s*>\s+\S",                     # 引用
            r"\[[^\]]+\]\([^)]+\)",               # 链接
            r"(?m)^\|.+\|\s*$",                    # 表格行
            r"(?m)^\s*---+\s*$",                   # 分隔线
        )
        return any(re.search(p, text) for p in patterns)

    def _update_title(self):
        name = self._provider.model_name if self._provider else CFG.llm_model
        self.title = f"Coding Agent - {CFG.llm_provider}: {name}"

    def _tick_clock(self):
        self.query_one("#clock_label", Label).update(
            datetime.now().strftime("%H:%M:%S"))

    def _refresh_todo(self):
        # 仅有待办时输出，避免空字符串污染聊天区
        todo_text = _todo_rich()
        if todo_text:
            self._chat_write(todo_text)

    def _refresh_metrics(self):
        self._chat_write(_metrics_rich())

    # ── ask_user 内联输入 ──

    def _hook_ask_user(self, question: str, default: str) -> str:
        """在聊天区显示追问，复用底部输入框等待用户回复。"""
        self._ask_result = [default or "(No response)"]
        self._ask_default = default
        self._ask_event = threading.Event()

        def enter_ask_mode():
            self._chat_write(f"\n[bold magenta]?[/]  {question}")
            if default:
                self._chat_write(f"[dim](Press Enter for default: {default})[/]")
            prompt = self.query_one("#prompt", _HistoryInput)
            self._original_placeholder = prompt.placeholder
            prompt.placeholder = (
                f"Your response... (Default: {default})" if default else "Your response...")
            prompt.disabled = False
            prompt.focus()
            
            loading_container = self.query_one("#loading_container")
            loading_container.remove_class("visible")
            
            self.query_one("#status_label", Label).update(
                "❓ [magenta]Waiting for response...[/]")

        self.call_from_thread(enter_ask_mode)
        self._ask_event.wait()  # 阻塞工作线程
        self._ask_event = None
        return self._ask_result[0]

    # ── 输入处理 ──

    def on_input_submitted(self, ev: Input.Submitted):
        if ev.input.id != "prompt":
            return
        raw = ev.value or ""
        text = raw.strip()
        # 中文注释：输入 / 前缀时，若只匹配一个命令，回车自动补全并执行。
        if text.startswith("/") and " " not in text:
            prompt = self.query_one("#prompt", _HistoryInput)
            auto = prompt.resolve_single_slash(text)
            if auto:
                text = auto
        ev.input.value = ""

        # 追问回复模式
        if self._ask_event is not None:
            answer = text if text else (self._ask_default or "")
            self._ask_result[0] = answer
            self._chat_write(f"[bold cyan]>[/]  {answer}")
            prompt = self.query_one("#prompt", _HistoryInput)
            prompt.placeholder = self._original_placeholder
            prompt.disabled = True
            
            loading_container = self.query_one("#loading_container")
            loading_container.add_class("visible")
            
            self.query_one("#status_label", Label).update(
                "[yellow]Working...[/]")
            self._ask_event.set()
            return

        # 常规输入
        if self.busy or not text:
            return
        if text.lower() in ("exit", "quit", "/q"):
            self.exit()
            return

        # 斜杠命令分发
        if text.lower().startswith("/"):
            if self._handle_slash(text):
                return

        self.query_one("#prompt", _HistoryInput).push(text)
        self._active_worker = self._send(text)

    def on_input_changed(self, ev: Input.Changed):
        """输入 / 前缀时，在状态栏显示命令建议。"""
        if ev.input.id != "prompt" or self.busy:
            return
        text = (ev.value or "").strip().lower()
        if text.startswith("/") and " " not in text:
            matches = [cmd for cmd in SLASH_COMMANDS if cmd.startswith(text)]
            if matches:
                preview = " ".join(matches[:6])
                if len(matches) > 6:
                    preview += " ..."
                self.query_one("#status_label", Label).update(
                    f"[cyan]命令建议:[/] {preview} [dim](Tab 自动补全)[/]"
                )
                return
        self.query_one("#status_label", Label).update("[green]Ready[/]")

    def _handle_slash(self, text: str) -> bool:
        """处理 TUI 斜杠命令，返回 True 表示已处理。"""
        low = text.lower()
        agent = self._agent
        handlers = {
            "/model": lambda: self._chat_write(
                f"[bold cyan]模型[/]\n{self._provider.model_name}"),
            "/workdir": lambda: self._chat_write(
                f"[bold cyan]工作目录[/]\n{CFG.workdir}"),
            "/hooks": lambda: self._chat_write(
                f"[bold cyan]激活的 Hook[/]\n{self._agent.hooks.summary()}"),
            "/stop": lambda: self.action_interrupt_generation(),
            "/skills": lambda: self._show_skills(),
            "/help": lambda: self._show_help(),
            "/todo": lambda: self._chat_write(
                f"[bold cyan]待办看板[/]\n{_todo_rich()}" if len(_todo_rich()) > 0 else "[bold cyan]待办看板[/]\n[dim]暂无待办[/]"),
            "/tasks": lambda: self._chat_write(
                f"[bold cyan]持久任务[/]\n{TASK_MGR.list_all()}"),
            "/bg": lambda: self._chat_write(
                f"[bold cyan]后台任务[/]\n{BG_MGR.check()}"),
            "/cron": lambda: self._chat_write(
                f"[bold cyan]定时任务[/]\n{SCHEDULER.list_tasks()}"),
            "/worktree": lambda: self._chat_write(
                f"[bold cyan]Worktree[/]\n{WT_MGR.list_all()}"),
            "/memories": lambda: self._show_memories(),
            "/prompt": lambda: self._show_prompt(),
            "/sections": lambda: self._show_sections(),
            "/export": lambda: self.action_export_chat(),
            "/clear": lambda: self.query_one("#chat", RichLog).clear(),
            "/clear_todo": lambda: (
                PLANNER._clear(),  # 使用 Planner 内置清理，确保写回 todo.json
                self._chat_write("[dim]已清空待办列表[/]"),
            ),
            "/metrics": lambda: self._refresh_metrics(),

   
        }

        cmd = low.split()[0]
        if cmd in handlers:
            handlers[cmd]()
            return True
        return False

    def _show_prompt(self):
        prompt = self._agent.prompt_builder.build()
        self._chat_write(
            f"[dim]--- System Prompt ({len(prompt)} chars) ---[/]")
        self._chat_write(prompt[:3000])
        if len(prompt) > 3000:
            self._chat_write(
                f"[dim]... 截断，共 {len(prompt)} 字符 ...[/]")
        self._chat_write("[dim]--- End ---[/]")

    def _show_sections(self):
        headers = self._agent.prompt_builder.section_headers()
        lines = ["[bold cyan]Prompt Sections[/]"]
        lines.extend(f"  {h}" for h in headers)
        self._chat_write("\n".join(lines))

    def _show_memories(self):
        entries = MEMORY_MGR.list_all()
        if entries:
            lines = ["[bold cyan]持久记忆[/]"]
            for e in entries:
                lines.append(
                    f"  [dim][{e.mem_type}][/] [yellow]{e.name}[/]: "
                    f"{e.description}")
            self._chat_write("\n".join(lines))
        else:
            self._chat_write(
                "[dim]暂无记忆。Agent 可通过 save_memory 工具创建。[/]")

    def _show_skills(self):
        skills = SKILLS.list_skills()
        if skills:
            lines = ["[bold cyan]可用 Skills[/]"]
            for s in skills:
                lines.append(f"  [yellow]/{s.name}[/]  — {s.description}")
            lines.append(
                "[dim]用法: 在输入中加 /skill-name 自动加载 skill 上下文[/]")
            self._chat_write("\n".join(lines))
        else:
            self._chat_write("[dim]暂无可用 Skill[/]")

    def _show_help(self):
        self._chat_write("""[bold cyan]可用命令:[/bold cyan]
  [yellow]/model[/]      显示当前模型
  [yellow]/workdir[/]    显示当前工作目录
  [yellow]/hooks[/]      显示激活的 Hook
  [yellow]/stop[/]       中断当前生成（等价 Ctrl+C）
  [yellow]/help[/]       显示此帮助信息
  [yellow]/q[/]          退出
  [yellow]/todo[/]       显示待办看板
  [yellow]/tasks[/]      显示持久任务
  [yellow]/bg[/]         显示后台任务
  [yellow]/cron[/]       显示定时任务
  [yellow]/worktree[/]   显示 Worktree 隔离通道
  [yellow]/memories[/]   显示持久记忆
  [yellow]/skills[/]     显示可用 Skills (/skill-name 内联加载)
  [yellow]/prompt[/]     显示当前系统提示词
  [yellow]/sections[/]   显示提示词各段标题
  [yellow]/export[/]     导出对话历史
  [yellow]/clear[/]      清屏     
  [yellow]/metrics[/]    显示指标面板""")

    # ── 对话发送 ──

    from textual import work
    
    @work(exclusive=True, thread=True)
    def _send(self, text: str):
        """在后台 Worker 中执行 Agent generator，直接消费流。"""
        self.call_from_thread(lambda: self._chat_write(f"\n[bold cyan]>[/]  [blue]{text}[/]"))
        self.busy = True
        self._interrupt_requested = False
        worker = get_current_worker()

        from ..llm.base import Message
        try:
            METRICS.user_messages += 1
            
            # 手动模拟 agent.chat() 的 generator 消费逻辑，但在 UI 中刷新
            self._agent._completion_check_done = False
            user_input = self._agent._process_skill_refs(text)
            self._agent.window.add(Message.user(user_input))

            for event in self._agent._query_loop_gen():
                # 中文注释：线程 worker 无法强杀，这里在每次事件边界检查取消请求并尽快退出。
                if worker.is_cancelled or self._interrupt_requested:
                    break
                if event["type"] == "system":
                    self.call_from_thread(lambda m=event["content"]: self._chat_write(f"[dim]{m}[/]"))
                elif event["type"] == "text":
                    self.call_from_thread(
                        lambda t=event["content"]: (
                            self._chat_write("\n[bold green]Assistant[/]"),
                            self._write_assistant_content(t),
                            # self._chat_write(f"[dim]{'─'*50}[/]"),
                        )
                    )

            if worker.is_cancelled or self._interrupt_requested:
                self.call_from_thread(
                    lambda: self._chat_write("[yellow]⏹ 已中断当前生成（将在当前阶段结束后停止）[/]")
                )

            self.call_from_thread(lambda: (
                setattr(self, "busy", False),
                self._refresh_todo(),
                _notify("Coding Agent", "Done ✓") if CFG.need_notify else None,
            ))
        except Exception as exc:
            self.call_from_thread(lambda e=exc: (
                setattr(self, "busy", False),
                self._chat_write(f"[bold red]Error[/] {e!s}"),
            ))
        finally:
            self._active_worker = None
            self._interrupt_requested = False

    # ── 快捷键 ──

    def action_interrupt_generation(self):
        """请求中断当前模型生成。"""
        if not self.busy:
            self._chat_write("[dim]当前没有进行中的生成任务[/]")
            return
        self._interrupt_requested = True
        if self._active_worker is not None:
            try:
                self._active_worker.cancel()
            except Exception:
                pass
        self.query_one("#status_label", Label).update("[yellow]Interrupting...[/]")
        self._chat_write("[yellow]正在请求中断当前生成...[/]")

    def action_switch_model(self):
        def on_selected(model: str | None):
            if not model or not self._provider:
                return
            old = self._provider.model_name
            self._provider.model_name = model
            self._update_title()
            log("INFO", "model_switched", f"{old} → {model}")
            self._chat_write(
                f"[dim]模型切换: {old} → [bold]{model}[/][/]")

        self.push_screen(ModelSwitchModal(
            self._provider.model_name if self._provider else ""),
            callback=on_selected)

    def action_export_chat(self):
        if self._agent:
            try:
                # Agent 当前没有 get_history()；直接导出上下文窗口中的消息。
                p = export_conversation(self._agent.window.messages)
                self._chat_write(f"[dim]已导出: {p}[/]")
            except Exception as exc:
                self._chat_write(f"[red]导出失败: {exc}[/]")

    def action_clear_chat(self):
        chat = self.query_one("#chat", RichLog)
        chat.clear()
        chat.write(
            f"[dim]{'─'*38}\n屏幕已清空（历史保留）\n{'─'*38}[/]")

    def action_refresh_panels(self):
        # We no longer have side panels, just pass
        pass

# ── 入口 ──

def run_tui():
    AgentTUIApp().run()
