# -*- coding: utf-8 -*-
"""
系统提示词构建管线。

核心思想：prompt 是管线，不是一个大字符串。

构建流程（6 段）：
  1. 核心指令          ← 稳定，极少修改
  2. 工具列表          ← 稳定，按注册表生成
  3. Skill 元数据      ← 稳定，按 skills/ 目录生成
  4. 记忆段落          ← 半稳定，按 memory/ 目录生成
  5. AGENTS.md 链      ← 半稳定，全局 → 项目 → 子目录
  ── DYNAMIC_BOUNDARY ──
  6. 动态上下文        ← 每轮变化（日期、模型、计划等）

静态段（1-5）可被 LLM 缓存以节省 token，动态段（6）每轮重建。
per-turn 提醒（todo reminder 等）作为独立 user 消息注入，不混入 system prompt。
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

from .config import CFG
from .memory.long_term import MEMORY_MGR
from .planner import PLANNER
from .skills import SKILLS
from .tasks import TASK_MGR
from .tools.registry import ToolRegistry
from .utils.log import log

DYNAMIC_BOUNDARY = "=== DYNAMIC_BOUNDARY ==="

# ── 记忆使用指南（Section 4 的一部分）──

MEMORY_GUIDANCE = """\
## 记忆使用指南
何时用 save_memory 保存：
- 用户声明偏好 → type: user
- 用户纠正你 → type: feedback
- 发现无法从当前代码推导的项目事实（合规要求、遗留模块不可动的业务原因）→ type: project
- 了解到外部资源位置（看板、仪表板、文档 URL）→ type: reference

不要保存：
- 代码结构（可从仓库重新读取）
- 临时任务状态（当前分支、PR 号、当前待办）
- 密钥或凭据"""

# ── 核心指令（Section 1）──

CORE_INSTRUCTIONS = """\
你是一个专业的 Coding Agent，具备强大的代码理解、编写和调试能力。

## 核心规则
- 优先使用工具完成任务，而不是口头描述步骤
- 修改文件前先用 read_file 阅读内容
- 遇到不确定的情况，用 ask_user 工具询问用户
- 多步任务请用 todo 工具管理计划

## 推理框架
每次行动前，先在回复开头用 <thinking> 标签简要分析：
1. 当前目标是什么？
2. 需要什么信息或操作？
3. 选择哪个工具，为什么？

## 待办（todo）使用规范
- 多步任务开始时，立即调用 todo 创建计划
- 开始执行某步前，将该条目改为 in_progress
- 每完成一步，必须立即将该条目改为 completed
- todo 是整表替换：每次传入全部条目
- **绝对禁止**在所有步骤都已完成后仍有条目处于 pending 或 in_progress

## 持久任务（task）使用规范
- 跨阶段/跨会话的大型工作用 task_create 创建
- 开始某任务前调用 task_update 将 status 改为 in_progress
- 完成后**必须立即**调用 task_update 将 status 改为 completed
- 在给出最终回复前，检查是否有已完成但未标记的任务

## 完成检查（最高优先级）
在你准备给出最终文字回复之前，必须执行以下检查：
1. todo 中是否有已完成但状态仍为 pending/in_progress 的条目？→ 立即调用 todo 更新
2. task 中是否有已完成但状态仍为 pending/in_progress 的任务？→ 立即调用 task_update 更新
只有确认所有状态都已正确更新后，才可以给出最终回复。\
"""


class SystemPromptBuilder:
    """
    分段管线式系统提示词构建器。

    每个 section 有单一来源和单一职责，便于推理、测试和演进。
    """

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        provider_name: str = "",
        model_name: str = "",
    ):
        self._tools = tool_registry
        self._provider_name = provider_name
        self._model_name = model_name

    def update_model_info(self, provider: str, model: str) -> None:
        """更新动态段中展示的 provider / 模型名（如切换模型后调用）。"""
        self._provider_name = provider
        self._model_name = model

    # ── Section 1: 核心指令 ──

    def _build_core(self) -> str:
        return CORE_INSTRUCTIONS

    # ── Section 2: 工具列表 ──

    def _build_tool_listing(self) -> str:
        if not self._tools:
            return ""
        specs = self._tools.all_specs()
        if not specs:
            return ""
        lines = ["# 可用工具"]
        for s in specs:
            props = s.parameters.get("properties", {})
            params = ", ".join(props.keys())
            lines.append(f"- {s.name}({params}): {s.description}")
        return "\n".join(lines)

    # ── Section 3: Skill 元数据 ──

    def _build_skill_listing(self) -> str:
        return SKILLS.inject_prompt()

    # ── Section 4: 记忆段落 + 使用指南 ──

    def _build_memory_section(self) -> str:
        parts: list[str] = []
        mem = MEMORY_MGR.load_prompt()
        if mem:
            parts.append(mem)
        parts.append(MEMORY_GUIDANCE.strip())
        return "\n\n".join(parts) if parts else ""

    # ── Section 5: AGENTS.md 链 ──

    def _build_agents_md(self) -> str:
        """
        按优先级加载 AGENTS.md：
          1. ~/.agent/AGENTS.md   （全局指令）
          2. <workdir>/AGENTS.md  （项目指令）
          3. <cwd>/AGENTS.md      （子目录指令，若 cwd != workdir）
        """
        sources: list[tuple[str, str]] = []

        global_md = Path.home() / ".agent" / "AGENTS.md"
        if global_md.exists():
            try:
                sources.append(("全局 (~/.agent/AGENTS.md)", global_md.read_text(encoding="utf-8")))
            except OSError:
                pass

        project_md = CFG.workdir / "AGENTS.md"
        if project_md.exists():
            try:
                sources.append(("项目根 (AGENTS.md)", project_md.read_text(encoding="utf-8")))
            except OSError:
                pass

        cwd = Path.cwd()
        if cwd != CFG.workdir:
            sub_md = cwd / "AGENTS.md"
            if sub_md.exists():
                try:
                    sources.append((f"子目录 ({cwd.name}/AGENTS.md)", sub_md.read_text(encoding="utf-8")))
                except OSError:
                    pass

        if not sources:
            return ""

        parts = ["# AGENTS.md 指令"]
        for label, content in sources:
            parts.append(f"## 来源: {label}")
            parts.append(content.strip())
        return "\n\n".join(parts)

    # ── Section 6: 动态上下文 ──

    def _build_dynamic_context(self) -> str:
        lines = [
            f"当前日期: {datetime.date.today().isoformat()}",
            f"工作目录: {CFG.workdir}",
            f"模型: {self._provider_name}/{self._model_name}",
            f"平台: {os.uname().sysname} {os.uname().machine}",
        ]

        if os.environ.get("AGENT_RAW_MODE") == "1":
            lines.append("\n[已启用 RAW 模式]\n"
                         "你正在 shell 管道中运行（stdin/stdout 已重定向）。\n"
                         "本轮必须严格遵守以下要求：\n"
                         "1. 只输出用户明确要求的最终内容（例如可执行的 Python 代码、原始 JSON 等）。\n"
                         "2. 不要输出任何闲聊或客套（例如“下面是你的代码”）。\n"
                         "3. 不要使用 Markdown 格式，不要用 ``` 包裹代码。\n"
                         "4. 除非用户明确要求，否则不要添加解释说明。")

        # 当前会话计划（todo）
        plan = PLANNER.render()
        if plan and plan != "尚无计划。":
            lines.append(f"\n[当前会话计划]\n{plan}")

        # 持久任务图（跨会话）
        task_summary = TASK_MGR.render_for_prompt()
        if task_summary:
            lines.append(f"\n{task_summary}")

        return "# 动态上下文\n" + "\n".join(lines)

    # ── 组装 ──

    def build(self) -> str:
        """
        组装完整的系统提示词。

        静态段（1-5）与动态段（6）以 DYNAMIC_BOUNDARY 分隔。
        静态前缀可被 LLM API 缓存以节省 prompt token。
        """
        sections = [self.get_static_context()]
        sections.append(DYNAMIC_BOUNDARY)

        dynamic = self._build_dynamic_context()
        if dynamic:
            sections.append(dynamic)

        prompt = "\n\n".join(sections)
        log("DEBUG", "system_prompt_built",
            f"chars={len(prompt)} sections={len(sections)}")
        return prompt

    def get_static_context(self) -> str:
        """
        获取包含 1-5 段的静态系统提示词部分。
        未来可扩展为带 TTL / MD5 校验的缓存。
        """
        if not hasattr(self, "_cached_static"):
            sections: list[str] = []
            for builder in (
                self._build_core,
                self._build_tool_listing,
                self._build_skill_listing,
                self._build_memory_section,
                self._build_agents_md,
            ):
                section = builder()
                if section:
                    sections.append(section)
            self._cached_static = "\n\n".join(sections)
        return self._cached_static

    def section_headers(self) -> list[str]:
        """返回当前 prompt 中所有 section 标题（用于 /sections 调试）。"""
        prompt = self.build()
        headers = []
        for line in prompt.splitlines():
            if line.startswith("# ") or line == DYNAMIC_BOUNDARY:
                headers.append(line)
        return headers


def build_system_reminder(extra: str = "") -> str | None:
    """
    构建 per-turn 系统提醒（作为 user 消息注入，不混入 system prompt）。

    用于短生命周期的上下文，如 todo 提醒、task 状态检查等。
    """
    parts: list[str] = []

    # todo 提醒
    rem = PLANNER.reminder()
    if rem:
        parts.append(rem)

    # task 状态提醒：有 in_progress 的任务时提醒更新
    in_progress_tasks = [
        t for t in TASK_MGR._all_tasks()
        if t.get("status") == "in_progress"
    ]
    if in_progress_tasks:
        names = ", ".join(f"#{t['id']}({t['subject']})" for t in in_progress_tasks)
        parts.append(
            f"[系统提醒] 以下任务仍为 in_progress: {names}。"
            "若已完成，请立即调用 task_update 标为 completed。"
        )

    if extra:
        parts.append(extra)

    if not parts:
        return None

    return "<system-reminder>\n" + "\n".join(parts) + "\n</system-reminder>"
