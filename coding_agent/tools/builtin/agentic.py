# -*- coding: utf-8 -*-
"""Agent 体系工具：tool_search + agent。"""

from __future__ import annotations

import json

from ...llm.base import LLMProvider
from ...tools.registry import ToolRegistry, tool

_provider: LLMProvider | None = None
_registry: ToolRegistry | None = None


def set_agent_runtime(provider: LLMProvider, registry: ToolRegistry) -> None:
    """由 bootstrap 注入运行时依赖。"""
    global _provider, _registry
    _provider = provider
    _registry = registry


@tool(
    name="tool_search",
    description="按关键词搜索可用工具（含延迟加载工具）",
    when_to_use="当你不确定该用哪个工具，或怀疑存在未展示的延迟工具时先调用它。",
    search_hint="find tool discover deferred hidden tools",
    parallel=False,
)
def tool_search(query: str, limit: int = 8) -> str:
    if _registry is None:
        return "Error: tool_search 未初始化"
    rows = _registry.search(query, include_enabled=True, include_deferred=True, limit=limit)
    tools: list[dict[str, str]] = []
    for td in rows:
        tools.append(
            {
                "name": td.name,
                "description": td.description,
                "when_to_use": td.when_to_use or td.description,
                "search_hint": td.search_hint,
                "deferred": "yes" if td.should_defer else "no",
            }
        )
    return json.dumps(
        {"query": query, "count": len(tools), "tools": tools},
        ensure_ascii=False,
        indent=2,
    )


def _run_agent(prompt: str, agent_type: str = "general", description: str = "") -> str:
    if _provider is None or _registry is None:
        return "Error: agent 工具未初始化"
    from ...agent import run_subagent

    normalized = (agent_type or "general").strip().lower()
    role_map = {
        "general": "通用执行代理",
        "explore": "代码探索代理（偏只读搜索）",
        "plan": "方案规划代理（偏架构与分解）",
        "verification": "验证代理（偏测试与检查）",
    }
    role_desc = role_map.get(normalized, role_map["general"])
    # 中文注释：把 agent_type 映射成明确的执行角色，帮助子代理聚焦任务范围。
    sub_prompt = (
        f"[子代理类型] {normalized}\n"
        f"[角色说明] {role_desc}\n"
        f"[任务描述] {description or '未提供'}\n\n"
        f"{prompt}"
    )
    return run_subagent(_provider, _registry, sub_prompt)


@tool(
    name="agent",
    description="委托给专用子代理执行任务（Explore/Plan/Verification/General）",
    when_to_use="当任务较复杂、需要并行思考或拆分成独立子问题时使用。",
    search_hint="subagent explore plan verification",
    should_defer=True,
    parallel=False,
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "交给子代理的完整任务描述"},
            "agent_type": {
                "type": "string",
                "enum": ["general", "explore", "plan", "verification"],
                "description": "子代理类型",
            },
            "description": {"type": "string", "description": "可选：任务简述"},
        },
        "required": ["prompt"],
    },
)
def agent_tool(prompt: str, agent_type: str = "general", description: str = "") -> str:
    return _run_agent(prompt=prompt, agent_type=agent_type, description=description)


# @tool(
#     name="task",
#     description="兼容旧版：等价于 agent 工具",
#     when_to_use="仅在模型/提示词仍使用旧名称 task 时使用。",
#     search_hint="legacy task alias agent",
#     should_defer=True,
#     parallel=False,
# )
# def task_alias(prompt: str, description: str = "子任务") -> str:
#     return _run_agent(prompt=prompt, agent_type="general", description=description)
