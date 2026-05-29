# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.agent import Agent
from agent.config import AgentConfig, CFG
from agent.context.checkpoint import SessionCheckpoint
from agent.context.window import ContextWindow
from agent.hooks import HookResult
from agent.llm.base import Message, ToolCall
from agent.llm.recovery import LLMRecoveryPolicy
from agent.tool_policy import ToolExecutionPolicy
from agent.tools.base import ToolDef
from agent.tools.registry import ToolRegistry
from agent.utils.trace import TraceRecorder


class FakeProvider:
    def __init__(self, responses):
        self.model_name = "fake-model"
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, *, tools=None, system=""):
        self.calls += 1
        if not self._responses:
            return Message.assistant("default")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def stream(self, messages, *, tools=None, system=""):
        return
        yield

    def count_tokens(self, text: str) -> int:
        return len(text)


class FakeCompactor:
    def __init__(self):
        self.calls = 0

    def compact(self, window, focus=None):
        self.calls += 1
        return [Message.user("compacted")]


class EmptyHooks:
    def run_hooks(self, event, context=None):
        return HookResult()


def make_policy(max_attempts: int = 3) -> LLMRecoveryPolicy:
    policy = LLMRecoveryPolicy(max_attempts=max_attempts)
    policy._backoff_delay = lambda attempt: 0  # type: ignore[method-assign]
    return policy


class LLMRecoveryPolicyTests(unittest.TestCase):
    def test_retries_transient_errors(self):
        provider = FakeProvider([RuntimeError("rate limit"), Message.assistant("ok")])
        policy = make_policy(max_attempts=2)
        window = ContextWindow([Message.user("hello")])

        response = policy.call(
            provider,
            window,
            FakeCompactor(),
            tools=[],
            system="system",
            build_system=lambda: "system",
            emit_system=lambda msg: None,
        )

        self.assertEqual(response.content, "ok")
        self.assertEqual(provider.calls, 2)

    def test_compacts_context_errors_before_retrying(self):
        provider = FakeProvider([RuntimeError("context_length exceeded"), Message.assistant("ok")])
        compactor = FakeCompactor()
        policy = make_policy(max_attempts=1)
        window = ContextWindow([Message.user("large")])

        response = policy.call(
            provider,
            window,
            compactor,
            tools=[],
            system="system",
            build_system=lambda: "rebuilt",
            emit_system=lambda msg: None,
        )

        self.assertEqual(response.content, "ok")
        self.assertEqual(compactor.calls, 1)
        self.assertEqual(window.messages[0].content, "compacted")

    def test_repairs_empty_model_response(self):
        provider = FakeProvider([Message.assistant(""), Message.assistant("fixed")])
        policy = make_policy(max_attempts=2)
        window = ContextWindow([Message.user("hello")])

        response = policy.call(
            provider,
            window,
            FakeCompactor(),
            tools=[],
            system="system",
            build_system=lambda: "system",
            emit_system=lambda msg: None,
        )

        self.assertEqual(response.content, "fixed")
        self.assertIn("格式无效", window.messages[-1].content)

    def test_detects_truncated_text(self):
        policy = make_policy()
        self.assertTrue(policy.needs_continuation(Message(role="assistant", content="x", finish_reason="max_tokens")))
        self.assertTrue(policy.needs_continuation(Message.assistant("x" * 7001)))
        self.assertFalse(policy.needs_continuation(Message.assistant("done.")))


class AgentLoopTests(unittest.TestCase):
    def test_chat_returns_final_text_without_tools(self):
        provider = FakeProvider([Message.assistant("final")])
        registry = ToolRegistry()
        seen_text: list[str] = []

        with patch("agent.agent.SCHEDULER.start", lambda: None):
            agent = Agent(provider, registry, hooks=EmptyHooks())
            agent.callbacks.on_text = seen_text.append
            result = agent.chat("hello")

        self.assertEqual(result, "final")
        self.assertEqual(seen_text, ["final"])

    def test_tool_search_discovery_enables_deferred_tool_next_turn(self):
        responses_seen_tool_counts: list[int] = []

        class DiscoveryProvider(FakeProvider):
            def chat(self, messages, *, tools=None, system=""):
                responses_seen_tool_counts.append(len(tools or []))
                return super().chat(messages, tools=tools, system=system)

        provider = DiscoveryProvider([
            Message.assistant(tool_calls=[
                ToolCall(
                    id="call-1",
                    name="tool_search",
                    arguments={"query": "deferred"},
                )
            ]),
            Message.assistant("done"),
        ])
        registry = ToolRegistry()
        registry.register(ToolDef(
            name="tool_search",
            description="search tools",
            fn=lambda query: '{"tools": [{"name": "deferred_tool"}]}',
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        ))
        registry.register(ToolDef(
            name="deferred_tool",
            description="deferred",
            fn=lambda: "ok",
            parameters={"type": "object", "properties": {}},
            should_defer=True,
        ))

        with patch("agent.agent.SCHEDULER.start", lambda: None):
            agent = Agent(provider, registry, hooks=EmptyHooks())
            result = agent.chat("find tool")

        self.assertEqual(result, "done")
        self.assertEqual(responses_seen_tool_counts, [1, 2])

    def test_max_rounds_returns_guardrail_message(self):
        old_max = CFG.max_tool_rounds
        CFG.max_tool_rounds = 1
        provider = FakeProvider([
            Message.assistant(tool_calls=[
                ToolCall(id="call-1", name="noop", arguments={})
            ])
        ])
        registry = ToolRegistry()
        registry.register(ToolDef(
            name="noop",
            description="noop",
            fn=lambda: "ok",
            parameters={"type": "object", "properties": {}},
        ))

        try:
            with patch("agent.agent.SCHEDULER.start", lambda: None):
                agent = Agent(provider, registry, hooks=EmptyHooks())
                result = agent.chat("loop")
        finally:
            CFG.max_tool_rounds = old_max

        self.assertIn("达到工具调用轮数上限", result)


class ConfigPolicyCheckpointTraceTests(unittest.TestCase):
    def test_agent_config_loads_structured_json_with_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent_config.json"
            path.write_text(json.dumps({
                "llm": {"provider": "openai", "model": "json-model"},
                "openai": {
                    "api_key": "json-key",
                    "base_url": "https://json.example/v1",
                },
            }), encoding="utf-8")
            old_model = os.environ.get("AGENT_MODEL")
            os.environ["AGENT_MODEL"] = "env-model"
            try:
                cfg = AgentConfig.load(path)
            finally:
                if old_model is None:
                    os.environ.pop("AGENT_MODEL", None)
                else:
                    os.environ["AGENT_MODEL"] = old_model

        self.assertEqual(cfg.llm_provider, "openai")
        self.assertEqual(cfg.llm_model, "env-model")
        self.assertEqual(cfg.openai_api_key, "json-key")

    def test_tool_policy_parallelizes_only_read_capabilities(self):
        policy = ToolExecutionPolicy()
        read_def = ToolDef(
            name="read_file",
            description="read",
            fn=lambda path: "",
            parameters={"type": "object", "properties": {}},
            parallel=True,
        )
        write_def = ToolDef(
            name="write_file",
            description="write",
            fn=lambda path, content: "",
            parameters={"type": "object", "properties": {}},
            parallel=True,
        )

        self.assertTrue(policy.can_run_parallel(read_def, ToolCall("1", "read_file", {"path": "a"})))
        self.assertFalse(policy.can_run_parallel(write_def, ToolCall("2", "write_file", {"path": "a"})))

    def test_tool_policy_requires_permission_for_high_risk_native_command(self):
        decision = ToolExecutionPolicy().check_permission("bash", {"command": "sudo reboot"})
        self.assertEqual(decision["behavior"], "ask")
        self.assertEqual(decision["intent"]["risk"], "high")

    def test_checkpoint_round_trips_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = SessionCheckpoint(Path(tmp) / "checkpoint.json")
            messages = [
                Message.user("hello"),
                Message.assistant(tool_calls=[ToolCall("call-1", "read_file", {"path": "a.py"})]),
                Message.tool_result("call-1", "read_file", "content"),
            ]
            checkpoint.save(messages)
            loaded = checkpoint.load()

        self.assertEqual(len(loaded), 3)
        self.assertEqual(loaded[1].tool_calls[0].name, "read_file")
        self.assertEqual(loaded[2].content, "content")

    def test_trace_recorder_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            trace = TraceRecorder(path)
            trace.record("turn_start", turn=1)
            payload = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertEqual(payload["event"], "turn_start")
        self.assertEqual(payload["turn"], 1)


if __name__ == "__main__":
    unittest.main()
