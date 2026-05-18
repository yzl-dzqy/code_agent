# -*- coding: utf-8 -*-
"""
应用启动引导：统一初始化 LLM Provider、工具注册表、Agent 实例。

CLI 和 TUI 共享此启动流程，避免重复编排代码。
典型用法：
    provider, registry, agent = create_agent(callbacks)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .agent import Agent, AgentCallbacks
from .config import CFG
from .llm import get_provider
from .llm.base import LLMProvider
from .tools import ToolRegistry
from .tools.builtin.agentic import set_agent_runtime
from .tools.builtin.image import set_llm_provider
from .tools.builtin.user import set_ask_user_hook
from .tools.registry import _REGISTRY, tool as tool_dec
from .mcp.integration import init_mcp_for_registry
from .utils import metrics as metrics_mod
from .utils.log import init_log, log

if TYPE_CHECKING:
    from typing import Callable


def init_runtime(*, session_id: str = "", enable_wait_indicator: bool = True):
    """
    初始化全局运行时环境（日志、指标会话 ID 等）。

    应在进程启动时调用一次。
    """
    sid = session_id or uuid.uuid4().hex
    metrics_mod.RUN_SESSION_ID = sid
    init_log(
        CFG.log_dir, CFG.log_file, CFG.log_enabled,
        enable_wait_indicator, sid,
    )
    log("INFO", "program_start",
        f"provider={CFG.llm_provider} model={CFG.llm_model} workdir={CFG.workdir}")
    return sid


def create_provider() -> LLMProvider:
    """根据配置创建 LLM Provider 实例。"""
    provider = get_provider(CFG)
    set_llm_provider(provider)
    return provider


def create_registry(provider: LLMProvider) -> ToolRegistry:
    """
    创建并填充工具注册表。

    包含三类工具：
      1. builtin/ 目录下的内置工具（fs、shell、net 等）
      2. 各子系统注册的工具（todo、task、memory、background、cron 等）
      3. 虚拟工具（compact、subagent），实际由 Agent 内部处理
    """
    registry = ToolRegistry()
    registry.load_builtins()

    # compact —— 虚拟工具，触发上下文压缩（实际逻辑在 Agent._execute_single 中）
    @tool_dec(name="compact", description="压缩对话上下文以释放空间")
    def _compact(focus: str = "") -> str:
        return "Compacting..."

    # 向 agent/tool_search 工具注入 provider + registry 运行时
    set_agent_runtime(provider, registry)

    # 虚拟工具通过 @tool_dec 注册到全局 _REGISTRY，需同步到实例
    registry._tools.update(_REGISTRY)

    # MCP：扫描 .claude-plugin，连接外部进程并把 mcp__* 工具并入注册表
    mcp_info = init_mcp_for_registry(registry)
    if mcp_info.connected_servers:
        log(
            "INFO",
            "mcp_ready",
            f"plugins={mcp_info.plugin_names} servers={mcp_info.connected_servers} "
            f"extra_tools={mcp_info.tool_count}",
        )
    return registry


def create_agent(
    callbacks: AgentCallbacks | None = None,
    *,
    ask_user_hook: Callable[[str, str], str] | None = None,
) -> tuple[LLMProvider, ToolRegistry, Agent]:
    """
    一站式创建 Agent 及其所有依赖。

    返回 (provider, registry, agent) 三元组，
    调用方可保留 provider 引用以便后续切换模型。
    """
    provider = create_provider()
    registry = create_registry(provider)

    if ask_user_hook:
        set_ask_user_hook(ask_user_hook)

    agent = Agent(provider, registry, callbacks)
    return provider, registry, agent
