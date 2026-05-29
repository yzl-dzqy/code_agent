# -*- coding: utf-8 -*-
"""工具基类定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..llm.base import ToolSpec


@dataclass
class ToolDef:
    """已注册的工具：包含执行函数 + 元数据。"""
    name: str
    description: str
    fn: Callable[..., str]
    parameters: dict[str, Any]  # JSON Schema
    parallel: bool = True
    required_params: list[str] = field(default_factory=list)
    # 工具使用说明，供 ToolSearchTool 返回给模型
    when_to_use: str = ""
    # 工具检索关键词（短文本）；为空时会回退到 name/description
    search_hint: str = ""
    # 延迟工具：默认不放进初始工具列表，需要通过 tool_search 发现
    should_defer: bool = False
    # 动态开关：例如依赖某环境变量时可按需禁用
    is_enabled: Callable[[], bool] | None = None

    def enabled(self) -> bool:
        """判断工具当前是否可用。"""
        if self.is_enabled is None:
            return True
        try:
            return bool(self.is_enabled())
        except Exception:
            return False

    def to_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            parallel=self.parallel,
        )
