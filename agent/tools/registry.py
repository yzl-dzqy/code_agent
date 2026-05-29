# -*- coding: utf-8 -*-
"""
工具注册表：@tool 装饰器 + 自动发现。

用法:
    @tool(name="read_file", description="读取文件")
    def read_file(path: str, limit: int | None = None) -> str:
        ...
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import Any, Callable, get_type_hints

from ..llm.base import ToolSpec
from .base import ToolDef

# 全局注册表
_REGISTRY: dict[str, ToolDef] = {}

# Python 类型 → JSON Schema 类型
_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _python_type_to_schema(tp: Any) -> dict[str, Any]:
    """将 Python 类型注解转换为 JSON Schema。"""
    origin = getattr(tp, "__origin__", None)

    # Optional[X] = Union[X, None]
    if origin is type(None):
        return {"type": "string"}

    args = getattr(tp, "__args__", None)
    # 处理 X | None 或 Optional[X]
    if origin is type(int | str):  # types.UnionType (Python 3.10+)
        non_none = [a for a in (args or []) if a is not type(None)]
        if len(non_none) == 1:
            return _python_type_to_schema(non_none[0])

    # 基本类型
    if tp in _TYPE_MAP:
        return {"type": _TYPE_MAP[tp]}

    # list[X]
    if origin is list and args:
        return {"type": "array", "items": _python_type_to_schema(args[0])}

    return {"type": "string"}


def _build_schema(fn: Callable, required_override: list[str] | None = None) -> tuple[dict, list[str]]:
    """从函数签名自动生成 JSON Schema + required 字段列表。"""
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        tp = hints.get(name, str)
        schema = _python_type_to_schema(tp)

        # 有 docstring 里的参数描述可以后续增强
        properties[name] = schema

        # 无默认值 = 必填
        if param.default is inspect.Parameter.empty:
            required.append(name)

    if required_override is not None:
        required = required_override

    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        result["required"] = required
    return result, required


def tool(
    name: str,
    description: str,
    *,
    parallel: bool = True,
    parameters: dict | None = None,
    required: list[str] | None = None,
    when_to_use: str = "",
    search_hint: str = "",
    should_defer: bool = False,
    is_enabled: Callable[[], bool] | None = None,
):
    """
    注册可调用工具：写入全局 _REGISTRY，供 ToolRegistry.load_builtins 合并。

    可显式传入 JSON Schema（parameters），否则从函数签名推断。
    """

    def decorator(fn: Callable) -> Callable:
        if parameters:
            schema = parameters
            req = required or parameters.get("required", [])
        else:
            schema, req = _build_schema(fn, required)

        td = ToolDef(
            name=name,
            description=description,
            fn=fn,
            parameters=schema,
            parallel=parallel,
            required_params=req,
            when_to_use=when_to_use,
            search_hint=search_hint,
            should_defer=should_defer,
            is_enabled=is_enabled,
        )
        _REGISTRY[name] = td
        fn._tool_def = td  # type: ignore[attr-defined]
        return fn

    return decorator


class ToolRegistry:
    """工具注册表，支持自动发现和执行。"""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, td: ToolDef):
        self._tools[td.name] = td

    def load_builtins(self):
        """自动发现并加载 builtin/ 下所有 @tool 装饰的函数，以及其他模块注册的工具。"""
        builtin_dir = Path(__file__).parent / "builtin"
        for _, module_name, _ in pkgutil.iter_modules([str(builtin_dir)]):
            importlib.import_module(f".builtin.{module_name}", package=__package__)
        # 确保外部模块注册的工具也被加载
        for mod in ("agent.planner", "agent.memory.long_term", "agent.skills", "agent.tasks", "agent.background", "agent.scheduler", "agent.worktree"):
            try:
                importlib.import_module(mod)
            except ImportError:
                pass
        self._tools.update(_REGISTRY)

    def get(self, name: str) -> ToolDef | None:
        td = self._tools.get(name)
        if td is None or not td.enabled():
            return None
        return td

    def find(self, name: str) -> ToolDef | None:
        """按名称查找工具，不做 enabled 过滤（用于检索展示元数据）。"""
        return self._tools.get(name)

    def execute(self, name: str, arguments: dict) -> str:
        """按注册名查找工具，仅传入函数签名中存在的参数后调用。"""
        td = self.get(name)
        if td is None:
            return f"Error: 未知工具 '{name}'"
        try:
            sig = inspect.signature(td.fn)
            # 只传递函数接受的参数
            valid = {k: v for k, v in arguments.items() if k in sig.parameters}
            result = td.fn(**valid)
            return str(result) if result is not None else "(无输出)"
        except Exception as exc:
            return f"Error: {exc}"

    def all_specs(
        self,
        *,
        exclude: set[str] | None = None,
        include_deferred: bool = False,
    ) -> list[ToolSpec]:
        excl = exclude or set()
        specs: list[ToolSpec] = []
        for td in self._tools.values():
            if td.name in excl:
                continue
            if not td.enabled():
                continue
            if td.should_defer and not include_deferred:
                continue
            specs.append(td.to_spec())
        return specs

    def search(
        self,
        query: str,
        *,
        include_enabled: bool = True,
        include_deferred: bool = True,
        limit: int = 8,
    ) -> list[ToolDef]:
        """按关键词检索工具元数据，供 ToolSearchTool 使用。"""
        q = (query or "").strip().lower()
        if not q:
            return []
        hits: list[tuple[int, ToolDef]] = []
        for td in self._tools.values():
            if td.should_defer and not include_deferred:
                continue
            if (not td.should_defer) and not include_enabled:
                continue
            haystack = " ".join(
                [
                    td.name,
                    td.description,
                    td.when_to_use,
                    td.search_hint,
                ]
            ).lower()
            if q not in haystack:
                continue
            # 关键词命中越精确，分数越低（优先级越高）
            score = 3
            if q in td.name.lower():
                score = 0
            elif q in td.search_hint.lower():
                score = 1
            elif q in td.when_to_use.lower():
                score = 2
            hits.append((score, td))
        hits.sort(key=lambda x: (x[0], x[1].name))
        return [td for _, td in hits[: max(1, limit)]]

    @property
    def names(self) -> list[str]:
        return list(self._tools.keys())
