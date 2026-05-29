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
  │       ├─ LLMRecoveryPolicy  LLM + 恢复   │
  │       ├─ recovery policy    续写 / 压缩   │
  │       ├─ _check_completion  完成态审计    │
  │       └─ _execute_tools     并行工具执行  │
  │           └─ _execute_single  Hook 集成   │
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

import json
import time
import re

from .agent_runtime import AgentCallbacks, AgentEvent, QueryState
from .config import CFG, SUBAGENT_PROMPT
from .context.compactor import Compactor
from .context.window import ContextWindow
from .hooks import HookManager
from .llm.base import LLMProvider, Message, ToolCall
from .llm.recovery import LLMRecoveryPolicy
from .llm.registry import ModelSelection, get_provider, normalize_provider, parse_model_ref
from .memory.long_term import MEMORY_MGR
from .background import BG_MGR
from .planner import PLANNER
from .prompt import SystemPromptBuilder, build_system_reminder
from .scheduler import SCHEDULER
from .skills import SKILLS
from .tasks import TASK_MGR
from .tool_executor import ToolExecutor
from .tools.registry import ToolRegistry
from .utils.log import log, preview_text, wait_run
from .utils.metrics import METRICS


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
        self._completion_check_done = False
        user_input = self._process_skill_refs(user_input)
        self.window.add(Message.user(user_input))
        
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
        return final_text

    def _query_loop_gen(self):
        """
        Generator 模式的 Agent 核心循环：
        逐步 yield 事件 (如 {"type": "text", "content": ...})，使得后续可接流式输出。
        """
        self.window.micro_compact(CFG.keep_recent_tool_results)
        
        state = QueryState()

        while state.turn_count <= CFG.max_tool_rounds:
            # 主动压缩：token 接近上限时不等 API 报错
            if self.window.needs_compaction(CFG.context_limit):
                log("INFO", "proactive_compact")
                yield AgentEvent("system", "[恢复] 上下文接近限制，主动压缩…")
                self.window.messages = self.compactor.compact(self.window)

            log("INFO", "agent_step",
                f"step={state.turn_count}/{CFG.max_tool_rounds} msgs={len(self.window.messages)}")

            # 1. 排空后台与定时通知队列
            self._drain_all_queues()

            # 2. 构建 System Prompt 并获取工具列表
            system = self.prompt_builder.build()
            tool_specs = self.tools.all_specs()
            # 如果模型通过 tool_search 发现了延迟工具，则在后续轮次显式开放
            for tool_name in sorted(state.discovered_tools):
                td = self.tools.find(tool_name)
                if td is None or not td.enabled():
                    continue
                if all(spec.name != td.name for spec in tool_specs):
                    tool_specs.append(td.to_spec())

            # 3. 执行模型调用（包含异常重试与网络退避机制）
            response = self.recovery.call(
                self.provider,
                self.window,
                self.compactor,
                tools=tool_specs,
                system=system,
                build_system=self.prompt_builder.build,
                emit_system=self._emit_system,
            )
            METRICS.record_model_turn()
            self._record_usage(response)

            # 4. 处理截断续写
            if self.recovery.needs_continuation(response):
                state.max_output_tokens_recovery_count += 1
                if state.max_output_tokens_recovery_count <= self.recovery.max_attempts:
                    log("INFO", "recovery_max_tokens", f"count={state.max_output_tokens_recovery_count}")
                    yield AgentEvent(
                        "system",
                        f"[恢复] 输出截断，续写中… "
                        f"({state.max_output_tokens_recovery_count}/{self.recovery.max_attempts})",
                    )
                    self.window.add(response)
                    self.window.add(Message.user(self.recovery.continuation_message))
                    continue
                log("WARN", "recovery_max_tokens_exhausted")
            
            state.max_output_tokens_recovery_count = 0

            # 5. 无工具调用时的结束检查
            if not response.tool_calls:
                nudge = self._check_completion_state()
                if nudge:
                    self.window.add(response)
                    self.window.add(Message.user(nudge))
                    log("INFO", "completion_check_injected")
                    state.transition_reason = "completion_check_nudge"
                    state.turn_count += 1
                    continue
                
                text = response.content.strip() or "（模型未返回有效内容，请重试）"
                self.window.add(response)
                yield AgentEvent("text", text)
                return

            # 6. 分离出 Tool_use 收集并分发给外部执行逻辑
            self.window.add(response)
            discovered = self._process_tool_calls(response.tool_calls)
            if discovered:
                state.discovered_tools.update(discovered)

            state.turn_count += 1
            state.transition_reason = "next_turn"

        final = f"达到工具调用轮数上限（{CFG.max_tool_rounds}），已停止。"
        self.window.add(Message.assistant(final))
        log("WARN", "max_steps_reached", str(CFG.max_tool_rounds))
        yield AgentEvent("text", final)

    # ─────────────────────────────────────────────────────────
    # 1.3 通知排空
    # ─────────────────────────────────────────────────────────

    def _drain_all_queues(self) -> None:
        """
        排空所有异步通知队列，将结果注入上下文。

        两个来源：
          - BackgroundManager: 后台命令执行完成
          - CronScheduler: 定时任务触发
        """
        # 后台任务完成通知
        bg_notifs = BG_MGR.drain()
        if bg_notifs:
            text = "\n".join(bg_notifs)
            self.window.add(Message.user(
                f"<background-results>\n{text}\n</background-results>"))
            log("INFO", "bg_notifications_injected", str(len(bg_notifs)))

        # 定时任务触发通知
        cron_notifs = SCHEDULER.drain()
        if cron_notifs:
            text = "\n".join(cron_notifs)
            self.window.add(Message.user(
                f"<scheduled-tasks>\n{text}\n</scheduled-tasks>"))
            log("INFO", "cron_notifications_injected", str(len(cron_notifs)))

    # ─────────────────────────────────────────────────────────
    # 1.4 最终回复与完成态审计
    # ─────────────────────────────────────────────────────────

    def _check_completion_state(self) -> str | None:
        """
        审计 todo 和 task 的完成态。

        仅触发一次（通过 _completion_check_done 标记），避免死循环。
        返回 None 表示状态正常，否则返回需注入的提醒消息。
        """
        if self._completion_check_done:
            return None
        self._completion_check_done = True

        issues: list[str] = []

        # 检查 todo：有未完成条目但没有 pending 的（说明工作可能已做完，忘记标记）
        items = PLANNER.state.items
        if items:
            not_done = [i for i in items if i.status != "completed"]
            pending = [i for i in items if i.status == "pending"]
            if not_done and not pending:
                names = ", ".join(f'"{i.content}"' for i in not_done)
                issues.append(
                    f"todo 中仍有未完成条目: {names}。"
                    "若已全部完成，请调用 todo 将它们标为 completed。")

        # 检查 task：有 in_progress 的持久任务
        in_progress = [
            t for t in TASK_MGR._all_tasks()
            if t.get("status") == "in_progress"
        ]
        if in_progress:
            names = ", ".join(f"#{t['id']}({t['subject']})" for t in in_progress)
            issues.append(
                f"持久任务仍为 in_progress: {names}。"
                "若已完成，请调用 task_update 标为 completed。")

        if not issues:
            return None

        return (
            "<system-reminder>\n"
            "[完成检查] 你即将给出最终回复，但以下状态未更新：\n"
            + "\n".join(f"- {i}" for i in issues)
            + "\n请先更新状态，再给出最终回复。\n"
            "</system-reminder>"
        )

    # ─────────────────────────────────────────────────────────
    # 1.5 工具执行
    # ─────────────────────────────────────────────────────────

    def _process_tool_calls(self, tool_calls: list[ToolCall]) -> set[str]:
        """执行工具调用、注入结果、更新提醒。"""
        had_todo = any(tc.name == "todo" for tc in tool_calls)
        results = self._tool_executor.execute_all(tool_calls)
        discovered_tools: set[str] = set()

        # 将结果逐条注入上下文
        for tc, result_text in zip(tool_calls, results):
            self.window.add(Message.tool_result(
                tool_call_id=tc.id, name=tc.name, content=result_text))
            if tc.name == "tool_search":
                discovered_tools.update(self._extract_discovered_tools(result_text))

        # todo 提醒：若本轮未调用 todo，记录跳过计数
        if not had_todo:
            PLANNER.note_round_without_update()

        # 注入 per-turn 系统提醒（todo 进度、task 状态等）
        reminder = build_system_reminder()
        if reminder:
            self.window.add(Message.user(reminder))
            log("INFO", "system_reminder_injected")
        return discovered_tools

    @staticmethod
    def _extract_discovered_tools(tool_result_text: str) -> set[str]:
        """从 tool_search 的 JSON 结果中提取可开放的工具名。"""
        try:
            payload = json.loads(tool_result_text)
            tools = payload.get("tools", [])
            if not isinstance(tools, list):
                return set()
            return {
                str(item.get("name", "")).strip()
                for item in tools
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            }
        except Exception:
            return set()

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

    def _record_usage(self, msg: Message) -> None:
        """记录 token 用量指标（兼容多家 provider 的字段名）。"""
        usage = getattr(msg, "_usage", None)
        if usage:
            p = (getattr(usage, "prompt_token_count", 0)
                 or getattr(usage, "prompt_tokens", 0) or 0)
            o = (getattr(usage, "candidates_token_count", 0)
                 or getattr(usage, "completion_tokens", 0) or 0)
            METRICS.record_tokens(p, o)


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
