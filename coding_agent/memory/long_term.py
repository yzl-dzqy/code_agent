# -*- coding: utf-8 -*-
"""
跨会话持久化记忆系统。

存储布局:
  .agent/memory/
    MEMORY.md          ← 自动生成的索引
    prefer_tabs.md     ← 独立记忆文件（带 frontmatter）
    review_style.md
    ...

记忆类型:
  - user:      用户偏好（"我喜欢用 tabs"、"总是用 pytest"）
  - feedback:  用户纠正（"不要做 X"、"上次错了因为…"）
  - project:   项目事实（非代码可推导的决策原因、合规要求等）
  - reference: 外部资源（看板地址、文档 URL、仪表板链接）

不应存储:
  - 代码结构（可从仓库重新读取）
  - 临时任务状态（当前分支、PR 号、当前待办）
  - 密钥/凭据
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import CFG
from ..tools.registry import tool
from ..utils.log import log

MEMORY_TYPES = ("user", "feedback", "project", "reference")
MAX_INDEX_LINES = 200


@dataclass
class MemoryEntry:
    name: str
    description: str
    mem_type: str
    content: str
    file: str  # 文件名


class MemoryManager:
    """
    分文件 Markdown 记忆管理器。

    每条记忆一个 .md 文件（带 frontmatter），外加一个自动生成的 MEMORY.md 索引。
    """

    def __init__(self, memory_dir: Path | None = None):
        self.memory_dir = memory_dir or (CFG.resolved_output_dir / "memory")
        self.memories: dict[str, MemoryEntry] = {}
        self._load_all()

    def _load_all(self) -> None:
        """扫描 memory_dir 下所有 .md 文件（MEMORY.md 除外）。"""
        self.memories = {}
        if not self.memory_dir.exists():
            return
        for md in sorted(self.memory_dir.glob("*.md")):
            if md.name == "MEMORY.md":
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed = self._parse_frontmatter(text)
            if not parsed:
                continue
            name = parsed.get("name", md.stem)
            self.memories[name] = MemoryEntry(
                name=name,
                description=parsed.get("description", ""),
                mem_type=parsed.get("type", "project"),
                content=parsed.get("content", ""),
                file=md.name,
            )
        if self.memories:
            log("INFO", "memory_loaded", f"{len(self.memories)} memories")

    def save(self, name: str, description: str, mem_type: str, content: str) -> str:
        """保存一条记忆并重建索引。"""
        if mem_type not in MEMORY_TYPES:
            return f"Error: type 必须是 {MEMORY_TYPES} 之一"
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
        if not safe_name:
            return "Error: 无效的记忆名称"

        self.memory_dir.mkdir(parents=True, exist_ok=True)
        frontmatter = (
            f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n"
            f"{content}\n"
        )
        file_name = f"{safe_name}.md"
        (self.memory_dir / file_name).write_text(frontmatter, encoding="utf-8")

        self.memories[name] = MemoryEntry(
            name=name, description=description,
            mem_type=mem_type, content=content, file=file_name,
        )
        self._rebuild_index()
        log("INFO", "memory_saved", f"{name} [{mem_type}]")
        return f"已保存记忆 '{name}' [{mem_type}] → {self.memory_dir / file_name}"

    def delete(self, name: str) -> str:
        """删除一条记忆。"""
        entry = self.memories.pop(name, None)
        if not entry:
            return f"Error: 未找到记忆 '{name}'"
        path = self.memory_dir / entry.file
        path.unlink(missing_ok=True)
        self._rebuild_index()
        log("INFO", "memory_deleted", name)
        return f"已删除记忆 '{name}'"

    def load_prompt(self) -> str:
        """生成注入 System Prompt 的记忆段落（按类型分组）。"""
        if not self.memories:
            return ""
        sections = ["# 持久记忆（跨会话）", ""]
        for mt in MEMORY_TYPES:
            typed = {k: v for k, v in self.memories.items() if v.mem_type == mt}
            if not typed:
                continue
            label = {"user": "用户偏好", "feedback": "用户反馈",
                     "project": "项目知识", "reference": "外部资源"}[mt]
            sections.append(f"## {label}")
            for name, entry in typed.items():
                sections.append(f"### {name}: {entry.description}")
                if entry.content.strip():
                    sections.append(entry.content.strip())
                sections.append("")
        return "\n".join(sections)

    def list_all(self) -> list[MemoryEntry]:
        return list(self.memories.values())

    def _rebuild_index(self) -> None:
        """重建 MEMORY.md 索引文件。"""
        lines = ["# Memory Index", ""]
        for name, entry in self.memories.items():
            lines.append(f"- {name}: {entry.description} [{entry.mem_type}]")
            if len(lines) >= MAX_INDEX_LINES:
                lines.append(f"... (截断于 {MAX_INDEX_LINES} 行)")
                break
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        (self.memory_dir / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _parse_frontmatter(text: str) -> dict | None:
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not m:
            return None
        result = {"content": m.group(2).strip()}
        for line in m.group(1).splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                result[key.strip()] = val.strip()
        return result


class DreamConsolidator:
    """
    记忆整合器（"Dream"）：在会话间自动合并、去重、裁剪记忆。

    7 道门控全部通过才执行：
      1. enabled 开关
      2. memory 目录存在且有记忆文件
      3. 非 plan 模式
      4. 距上次整合 ≥ 24h
      5. 距上次扫描 ≥ 10min
      6. 累计 ≥ 5 次会话
      7. 无活跃锁文件

    4 阶段整合流程：
      Orient → Gather → Consolidate → Prune
    """

    COOLDOWN_SECONDS = 86400
    SCAN_THROTTLE_SECONDS = 600
    MIN_SESSION_COUNT = 5
    LOCK_STALE_SECONDS = 3600

    PHASES = [
        "Orient: 扫描 MEMORY.md 索引，了解结构和分类",
        "Gather: 读取各记忆文件获取完整内容",
        "Consolidate: 合并相关记忆，移除过时条目",
        "Prune: 强制执行 200 行索引限制",
    ]

    def __init__(self, memory_dir: Path | None = None):
        self.memory_dir = memory_dir or (CFG.resolved_output_dir / "memory")
        self.lock_file = self.memory_dir / ".dream_lock"
        self.enabled = True
        self.mode = "default"
        self.last_consolidation_time = 0.0
        self.last_scan_time = 0.0
        self.session_count = 0

    def should_consolidate(self) -> tuple[bool, str]:
        """检查 7 道门控，全部通过返回 True。"""
        now = time.time()

        if not self.enabled:
            return False, "Gate 1: 整合已禁用"
        if not self.memory_dir.exists():
            return False, "Gate 2: memory 目录不存在"
        mds = [f for f in self.memory_dir.glob("*.md") if f.name != "MEMORY.md"]
        if not mds:
            return False, "Gate 2: 无记忆文件"
        if self.mode == "plan":
            return False, "Gate 3: plan 模式不允许整合"
        elapsed = now - self.last_consolidation_time
        if elapsed < self.COOLDOWN_SECONDS:
            return False, f"Gate 4: 冷却中，剩余 {int(self.COOLDOWN_SECONDS - elapsed)}s"
        scan_elapsed = now - self.last_scan_time
        if scan_elapsed < self.SCAN_THROTTLE_SECONDS:
            return False, f"Gate 5: 扫描节流，剩余 {int(self.SCAN_THROTTLE_SECONDS - scan_elapsed)}s"
        if self.session_count < self.MIN_SESSION_COUNT:
            return False, f"Gate 6: 仅 {self.session_count} 次会话，需 {self.MIN_SESSION_COUNT}"
        if not self._acquire_lock():
            return False, "Gate 7: 其他进程持有锁"
        return True, "全部 7 道门控通过"

    def consolidate(self) -> list[str]:
        """执行 4 阶段整合。"""
        ok, reason = self.should_consolidate()
        if not ok:
            log("INFO", "dream_skip", reason)
            return []

        log("INFO", "dream_start")
        self.last_scan_time = time.time()
        completed = []
        for i, phase in enumerate(self.PHASES, 1):
            log("INFO", "dream_phase", f"{i}/4: {phase}")
            completed.append(phase)

        self.last_consolidation_time = time.time()
        self._release_lock()
        log("INFO", "dream_done", f"{len(completed)} phases")
        return completed

    def _acquire_lock(self) -> bool:
        if self.lock_file.exists():
            try:
                pid_s, ts_s = self.lock_file.read_text().strip().split(":", 1)
                pid, lock_time = int(pid_s), float(ts_s)
                if (time.time() - lock_time) > self.LOCK_STALE_SECONDS:
                    self.lock_file.unlink()
                else:
                    try:
                        os.kill(pid, 0)
                        return False
                    except OSError:
                        self.lock_file.unlink()
            except (ValueError, OSError):
                self.lock_file.unlink(missing_ok=True)
        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            self.lock_file.write_text(f"{os.getpid()}:{time.time()}")
            return True
        except OSError:
            return False

    def _release_lock(self) -> None:
        """释放本进程持有的 Dream 整合锁文件。"""
        try:
            if self.lock_file.exists():
                pid_s = self.lock_file.read_text().strip().split(":")[0]
                if int(pid_s) == os.getpid():
                    self.lock_file.unlink()
        except (ValueError, OSError):
            pass


# ── 全局单例 ──

MEMORY_MGR = MemoryManager()
DREAM = DreamConsolidator()


# ── 注册为工具 ──

@tool(
    name="save_memory",
    description="保存一条跨会话持久记忆（user=偏好/feedback=纠正/project=项目知识/reference=外部资源）",
    parallel=False,
)
def save_memory(name: str, description: str, type: str, content: str) -> str:
    """保存持久记忆。type 必须是 user/feedback/project/reference 之一。"""
    return MEMORY_MGR.save(name, description, type, content)


@tool(
    name="delete_memory",
    description="删除一条持久记忆",
    parallel=False,
)
def delete_memory(name: str) -> str:
    return MEMORY_MGR.delete(name)


@tool(
    name="list_memories",
    description="列出所有已保存的持久记忆",
)
def list_memories() -> str:
    entries = MEMORY_MGR.list_all()
    if not entries:
        return "（暂无记忆）"
    lines = []
    for e in entries:
        lines.append(f"[{e.mem_type}] {e.name}: {e.description}")
    return "\n".join(lines)
