# -*- coding: utf-8 -*-
"""Single-query runner for the ReAct agent loop."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .agent_runtime import AgentEvent, QueryState
from .background import BG_MGR
from .config import CFG
from .llm.base import Message, ToolCall
from .planner import PLANNER
from .prompt import build_system_reminder
from .scheduler import SCHEDULER
from .tasks import TASK_MGR
from .utils.log import log
from .utils.metrics import METRICS

if TYPE_CHECKING:
    from .agent import Agent


class AgentRunner:
    """Runs one user query against an Agent's dependencies and context."""

    def __init__(self, agent: Agent):
        self.agent = agent
        self._completion_check_done = False

    def run(self):
        """Yield AgentEvent objects until the query reaches a final answer."""
        agent = self.agent
        agent.window.micro_compact(CFG.keep_recent_tool_results)

        state = QueryState()

        while state.turn_count <= CFG.max_tool_rounds:
            if agent.window.needs_compaction(CFG.context_limit):
                log("INFO", "proactive_compact")
                yield AgentEvent("system", "[恢复] 上下文接近限制，主动压缩…")
                agent.window.messages = agent.compactor.compact(agent.window)

            log(
                "INFO",
                "agent_step",
                f"step={state.turn_count}/{CFG.max_tool_rounds} msgs={len(agent.window.messages)}",
            )
            agent.trace.record(
                "turn_start",
                turn=state.turn_count,
                message_count=len(agent.window.messages),
            )

            self._drain_all_queues()

            system = agent.prompt_builder.build()
            tool_specs = agent.tools.all_specs()
            for tool_name in sorted(state.discovered_tools):
                td = agent.tools.find(tool_name)
                if td is None or not td.enabled():
                    continue
                if all(spec.name != td.name for spec in tool_specs):
                    tool_specs.append(td.to_spec())

            response = agent.recovery.call(
                agent.provider,
                agent.window,
                agent.compactor,
                tools=tool_specs,
                system=system,
                build_system=agent.prompt_builder.build,
                emit_system=agent._emit_system,
            )
            METRICS.record_model_turn()
            self._record_usage(response)
            agent.trace.record(
                "model_response",
                turn=state.turn_count,
                content_chars=len(response.content or ""),
                tool_calls=[tc.name for tc in (response.tool_calls or [])],
                finish_reason=response.finish_reason,
            )

            if agent.recovery.needs_continuation(response):
                state.max_output_tokens_recovery_count += 1
                if state.max_output_tokens_recovery_count <= agent.recovery.max_attempts:
                    log("INFO", "recovery_max_tokens", f"count={state.max_output_tokens_recovery_count}")
                    yield AgentEvent(
                        "system",
                        f"[恢复] 输出截断，续写中… "
                        f"({state.max_output_tokens_recovery_count}/{agent.recovery.max_attempts})",
                    )
                    agent.window.add(response)
                    agent.window.add(Message.user(agent.recovery.continuation_message))
                    continue
                log("WARN", "recovery_max_tokens_exhausted")

            state.max_output_tokens_recovery_count = 0

            if not response.tool_calls:
                nudge = self._check_completion_state()
                if nudge:
                    agent.window.add(response)
                    agent.window.add(Message.user(nudge))
                    log("INFO", "completion_check_injected")
                    state.transition_reason = "completion_check_nudge"
                    state.turn_count += 1
                    continue

                text = response.content.strip() or "（模型未返回有效内容，请重试）"
                agent.window.add(response)
                agent.trace.record("final", turn=state.turn_count, chars=len(text))
                agent.save_checkpoint()
                yield AgentEvent("text", text)
                return

            agent.window.add(response)
            discovered = self._process_tool_calls(response.tool_calls)
            if discovered:
                state.discovered_tools.update(discovered)

            state.turn_count += 1
            state.transition_reason = "next_turn"

        final = f"达到工具调用轮数上限（{CFG.max_tool_rounds}），已停止。"
        agent.window.add(Message.assistant(final))
        log("WARN", "max_steps_reached", str(CFG.max_tool_rounds))
        agent.trace.record("max_steps_reached", max_tool_rounds=CFG.max_tool_rounds)
        agent.save_checkpoint()
        yield AgentEvent("text", final)

    def _drain_all_queues(self) -> None:
        agent = self.agent
        bg_notifs = BG_MGR.drain()
        if bg_notifs:
            text = "\n".join(bg_notifs)
            agent.window.add(Message.user(f"<background-results>\n{text}\n</background-results>"))
            log("INFO", "bg_notifications_injected", str(len(bg_notifs)))

        cron_notifs = SCHEDULER.drain()
        if cron_notifs:
            text = "\n".join(cron_notifs)
            agent.window.add(Message.user(f"<scheduled-tasks>\n{text}\n</scheduled-tasks>"))
            log("INFO", "cron_notifications_injected", str(len(cron_notifs)))

    def _check_completion_state(self) -> str | None:
        if self._completion_check_done:
            return None
        self._completion_check_done = True

        issues: list[str] = []

        items = PLANNER.state.items
        if items:
            not_done = [i for i in items if i.status != "completed"]
            pending = [i for i in items if i.status == "pending"]
            if not_done and not pending:
                names = ", ".join(f'"{i.content}"' for i in not_done)
                issues.append(
                    f"todo 中仍有未完成条目: {names}。"
                    "若已全部完成，请调用 todo 将它们标为 completed。"
                )

        in_progress = [
            t for t in TASK_MGR._all_tasks()
            if t.get("status") == "in_progress"
        ]
        if in_progress:
            names = ", ".join(f"#{t['id']}({t['subject']})" for t in in_progress)
            issues.append(
                f"持久任务仍为 in_progress: {names}。"
                "若已完成，请调用 task_update 标为 completed。"
            )

        if not issues:
            return None

        return (
            "<system-reminder>\n"
            "[完成检查] 你即将给出最终回复，但以下状态未更新：\n"
            + "\n".join(f"- {i}" for i in issues)
            + "\n请先更新状态，再给出最终回复。\n"
            "</system-reminder>"
        )

    def _process_tool_calls(self, tool_calls: list[ToolCall]) -> set[str]:
        agent = self.agent
        had_todo = any(tc.name == "todo" for tc in tool_calls)
        results = agent._tool_executor.execute_all(tool_calls)
        discovered_tools: set[str] = set()

        for tc, result_text in zip(tool_calls, results):
            agent.window.add(Message.tool_result(
                tool_call_id=tc.id,
                name=tc.name,
                content=result_text,
            ))
            if tc.name == "tool_search":
                discovered_tools.update(self._extract_discovered_tools(result_text))
            agent.trace.record(
                "tool_result",
                tool=tc.name,
                result_chars=len(result_text),
            )

        if not had_todo:
            PLANNER.note_round_without_update()

        reminder = build_system_reminder()
        if reminder:
            agent.window.add(Message.user(reminder))
            log("INFO", "system_reminder_injected")
        return discovered_tools

    @staticmethod
    def _extract_discovered_tools(tool_result_text: str) -> set[str]:
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

    @staticmethod
    def _record_usage(msg: Message) -> None:
        usage = getattr(msg, "_usage", None)
        if usage:
            p = (
                getattr(usage, "prompt_token_count", 0)
                or getattr(usage, "prompt_tokens", 0)
                or 0
            )
            o = (
                getattr(usage, "candidates_token_count", 0)
                or getattr(usage, "completion_tokens", 0)
                or 0
            )
            METRICS.record_tokens(p, o)
