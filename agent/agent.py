# -*- coding: utf-8 -*-
"""
ReAct Agent 核心循环。

架构概览：
  ┌──────────────────────────────────────────┐
  │ Agent                                    │
  │  .chat(user_input)                       │
  │   ├─ _process_skill_refs  解析 /skill    │
  │   └─ _run_loop            主循环         │
  │       ├─ _drain_all_queues  排空通知     │
  │       └─ AgentRunner.run    单次 query 循环│
  └──────────────────────────────────────────┘

各子系统的职责边界：
  - background.py  运行槽位（谁在跑）
  - scheduler.py   定时调度（什么时候做）
  - tasks.py       持久任务（要完成什么，跨会话）
  - worktree.py    隔离执行面（在哪做，目录+分支隔离）
  - planner.py     会话内 todo（当前会话的步骤计划）
  - prompt.py      系统提示词构建管线
  - hooks.py       外部脚本钩子（PreToolUse/PostToolUse）
"""

from __future__ import annotations

import time
import re

from .agent_runtime import AgentCallbacks
from .agent_runner import AgentRunner
from .config import CFG, SUBAGENT_PROMPT
from .context.checkpoint import SessionCheckpoint
from .context.compactor import Compactor
from .context.window import ContextWindow
from .hooks import HookManager
from .llm.base import LLMProvider, Message
from .llm.recovery import LLMRecoveryPolicy
from .llm.registry import ModelSelection, get_provider, normalize_provider, parse_model_ref
from .memory.long_term import MEMORY_MGR
from .prompt import SystemPromptBuilder
from .scheduler import SCHEDULER
from .skills import SKILLS
from .tool_executor import ToolExecutor
from .tools.registry import ToolRegistry
from .utils.log import log, preview_text, wait_run
from .utils.metrics import METRICS
from .utils.trace import TraceRecorder


# ── 1. Agent 主类 ──

class Agent:
    """
    ReAct Agent：Think → Act → Observe → Reflect。

    支持并行工具调用、流式输出、错误自动恢复。
    """

    def __init__(
        self,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        callbacks: AgentCallbacks | None = None,
        hooks: HookManager | None = None,
    ):
        # 核心依赖
        self.provider = provider
        self.provider_name = normalize_provider(CFG.llm_provider)
        self.tools = tool_registry
        self.callbacks = callbacks or AgentCallbacks()

        # 外部脚本钩子（.hooks.json 配置的 PreToolUse/PostToolUse）
        self.hooks = hooks or HookManager(
            timeout=CFG.hook_timeout,
            sdk_mode=CFG.hook_sdk_mode,
        )

        # 上下文管理
        self.window = ContextWindow()       # 滑动窗口消息历史
        self.memory = MEMORY_MGR            # 持久记忆（.agent/memory/）
        self.compactor = Compactor(provider) # 上下文压缩器
        self.recovery = LLMRecoveryPolicy()
        self.checkpoint = SessionCheckpoint(
            CFG.session_checkpoint_file,
            enabled=CFG.session_checkpoint_enabled,
        )
        self.trace = TraceRecorder(CFG.trace_file, enabled=CFG.trace_enabled)
        if CFG.session_resume_on_start:
            self.window.messages = self.checkpoint.load()

        # 并行工具执行器分离（类似 claude-code-cli 中的 StreamingToolExecutor）
        self._tool_executor = ToolExecutor(
            tools=self.tools,
            callbacks=self.callbacks,
            hooks=self.hooks,
            window=self.window,
            compactor=self.compactor,
        )

        # 系统提示词管线（分段构建，支持 LLM 缓存）
        self.prompt_builder = SystemPromptBuilder(
            tool_registry=tool_registry,
            provider_name=self.provider_name,
            model_name=provider.model_name,
        )

        # 完成态检查标记（每次 chat 调用重置，防止死循环）
        self._completion_check_done = False

        # 启动子系统
        SCHEDULER.start()
        self._fire_session_start()

    def switch_model(self, model_ref: str, *, provider_name: str | None = None) -> tuple[ModelSelection, ModelSelection]:
        """
        运行时切换 provider/model。

        与直接修改 provider.model_name 不同，这里会按 provider 重建 SDK client，
        并同步压缩器、子代理工具和系统提示词中的模型信息。
        """
        if provider_name:
            selection = ModelSelection(normalize_provider(provider_name), model_ref.strip())
            if not selection.model:
                raise ValueError("模型名称不能为空")
        else:
            selection = parse_model_ref(model_ref, self.provider_name)

        old = ModelSelection(self.provider_name, self.provider.model_name)
        new_provider = get_provider(
            CFG,
            provider_name=selection.provider,
            model_name=selection.model,
        )

        self.provider = new_provider
        self.provider_name = selection.provider
        CFG.llm_provider = selection.provider
        CFG.llm_model = selection.model

        self.compactor = Compactor(new_provider)
        self._tool_executor.compactor = self.compactor
        self.prompt_builder.update_model_info(selection.provider, selection.model)

        from .tools.builtin.agentic import set_agent_runtime
        from .tools.builtin.image import set_llm_provider
        set_llm_provider(new_provider)
        set_agent_runtime(new_provider, self.tools)

        log("INFO", "model_switched", f"{old.ref} → {selection.ref}")
        return old, selection

    # ─────────────────────────────────────────────────────────
    # 1.1 公开接口
    # ─────────────────────────────────────────────────────────

    def chat(self, user_input: str) -> str:
        """
        同步对话入口。

        流程：解析 /skill 引用 → 注入消息 → 进入主循环。
        内部改用 generator 模式驱动。
        """
        user_input = self._process_skill_refs(user_input)
        self.window.add(Message.user(user_input))
        self.trace.record("user_message", chars=len(user_input))
        
        # 消费 generator 并回调
        final_text = ""
        for event in self._query_loop_gen():
            if event["type"] == "text":
                # 对于完整的模型回复或最终返回
                final_text = event["content"]
            elif event["type"] == "system":
                self._emit_system(event["content"])
        
        # 仅最终向 CLI / 回调暴露纯净的 text
        if final_text:
            self._emit_text(final_text)
        self.save_checkpoint()
        return final_text

    def _query_loop_gen(self):
        """Compatibility wrapper used by TUI; delegates to AgentRunner."""
        yield from AgentRunner(self).run()

    def save_checkpoint(self) -> None:
        """Persist current short-term context for optional later resume."""
        self.checkpoint.save(self.window.messages)

    def restore_checkpoint(self) -> int:
        """Restore saved short-term context; returns restored message count."""
        messages = self.checkpoint.load()
        self.window.messages = messages
        return len(messages)

    # ─────────────────────────────────────────────────────────
    # 1.6 辅助方法
    # ─────────────────────────────────────────────────────────

    def _process_skill_refs(self, text: str) -> str:
        """
        解析用户输入中的 /skill-name 引用。

        将匹配到的 skill 全文注入消息前面，剩余文本拼接在后。
        """
        loaded: list[str] = []
        rest: list[str] = []

        for token in text.split():
            m = re.match(r"^/([a-zA-Z0-9_-]+)$", token)
            if m:
                content = SKILLS.load_full_text(m.group(1))
                if not content.startswith("Error:"):
                    loaded.append(content)
                    log("INFO", "skill_inline_loaded", m.group(1))
                    if self.callbacks.on_system:
                        self.callbacks.on_system(f"已加载 skill: {m.group(1)}")
                    continue
            rest.append(token)

        if not loaded:
            return text
        return "\n\n".join(loaded) + "\n\n" + " ".join(rest)

    def _fire_session_start(self) -> None:
        """触发 SessionStart hook，注入外部脚本的初始消息。"""
        result = self.hooks.run_hooks(
            "SessionStart", {"tool_name": "", "tool_input": {}})
        for msg in result.messages:
            self.window.add(Message.user(f"[Hook]: {msg}"))
            log("INFO", "hook_session_msg", msg[:200])

    def _emit_text(self, text: str) -> None:
        """通知 UI 层显示最终回复文本。"""
        if self.callbacks.on_text:
            self.callbacks.on_text(text)

    def _emit_system(self, msg: str) -> None:
        """通知 UI 层显示系统级消息。"""
        log("INFO", "system_msg", msg)
        if self.callbacks.on_system:
            self.callbacks.on_system(msg)

# ── 3. 子代理 ──

def run_subagent(provider: LLMProvider, tools: ToolRegistry, prompt: str) -> str:
    """
    在独立上下文中运行子代理。

    子代理不使用 compact/todo/task 工具，执行完毕后返回简洁摘要。
    用于将复杂任务分解为可独立完成的子任务。
    """
    mcp_names = {n for n in tools.names if n.startswith("mcp__")}
    sub_specs = tools.all_specs(exclude={"compact", "todo", "task"} | mcp_names)
    msgs = [Message.user(
        "请完成以下任务。需要时用工具；结束时给出简洁中文摘要。\n\n" + prompt
    )]

    log("INFO", "subagent_start", preview_text(prompt, 200))

    for step in range(CFG.subagent_max_steps):
        resp = wait_run(
            "子代理",
            lambda: provider.chat(msgs, tools=sub_specs, system=SUBAGENT_PROMPT),
        )
        METRICS.record_model_turn()

        if not resp.tool_calls:
            text = resp.content.strip()
            log("INFO", "subagent_done", f"step={step+1} chars={len(text)}")
            return text[:CFG.subagent_result_max_chars] if text else "(无摘要)"

        msgs.append(resp)
        for tc in resp.tool_calls:
            result = tools.execute(tc.name, tc.arguments)
            msgs.append(Message.tool_result(tc.id, tc.name, result))

    return "(子代理达到步数上限)"[:CFG.subagent_result_max_chars]


# ── 4. 对话导出 ──

def export_conversation(messages: list[Message]) -> str:
    """将对话历史导出为 JSON Lines 文件，返回文件路径。"""
    from .utils.metrics import RUN_SESSION_ID
    export_dir = CFG.resolved_output_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"session_{RUN_SESSION_ID or 'run'}_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(
                {"role": msg.role, "content": msg.content},
                ensure_ascii=False, default=str,
            ) + "\n")
    log("INFO", "export_conversation", str(path))
    return str(path)
