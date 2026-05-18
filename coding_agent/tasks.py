# -*- coding: utf-8 -*-
"""
持久任务图：跨会话的工作项管理。

与 todo（会话内轻量计划）互补：
  - todo：会话内临时规划，压缩后消失
  - task：持久化到磁盘，带依赖图，跨会话存活

存储布局：
  .agent/tasks/
    task_1.json   {"id":1, "subject":"...", "status":"completed", ...}
    task_2.json   {"id":2, "blockedBy":[1], "status":"pending", ...}

依赖关系：
  - blockedBy: 前置任务列表（全部完成后此任务可执行）
  - blocks:    后续任务列表（此任务完成后解锁）
  - 完成某任务时自动从其他任务的 blockedBy 中移除
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import CFG
from .tools.registry import tool
from .utils.log import log


class TaskManager:
    """持久任务图的 CRUD 管理器。"""

    def __init__(self, tasks_dir: Path | None = None):
        self.dir = tasks_dir or CFG.tasks_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        """扫描磁盘获取当前最大任务 ID。"""
        ids = []
        for f in self.dir.glob("task_*.json"):
            try:
                ids.append(int(f.stem.split("_")[1]))
            except (IndexError, ValueError):
                pass
        return max(ids) if ids else 0

    def _path(self, task_id: int) -> Path:
        """返回任务 JSON 文件路径。"""
        return self.dir / f"task_{task_id}.json"

    def _load(self, task_id: int) -> dict:
        p = self._path(task_id)
        if not p.exists():
            raise ValueError(f"任务 #{task_id} 不存在")
        return json.loads(p.read_text(encoding="utf-8"))

    def _save(self, task: dict) -> None:
        self._path(task["id"]).write_text(
            json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")

    def _all_tasks(self) -> list[dict]:
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            try:
                tasks.append(json.loads(f.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        return tasks

    # ── CRUD ──

    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "worktree": "",      # 绑定的 worktree 名称（空=未绑定）
            "blockedBy": [],
            "blocks": [],
        }
        self._save(task)
        log("INFO", "task_created", f"#{self._next_id}: {subject}")
        self._next_id += 1
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def update(
        self,
        task_id: int,
        status: str = "",
        add_blocked_by: list[int] | None = None,
        add_blocks: list[int] | None = None,
    ) -> str:
        task = self._load(task_id)

        if status:
            valid = ("pending", "in_progress", "completed", "deleted")
            if status not in valid:
                return f"Error: status 必须是 {valid} 之一"
            task["status"] = status
            if status == "completed":
                self._clear_dependency(task_id)
                log("INFO", "task_completed", f"#{task_id}")

        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))

        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
            # 不变量：A.blocks 含 B 则 B.blockedBy 须含 A（依赖图双向一致）
            for bid in add_blocks:
                try:
                    blocked = self._load(bid)
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        self._save(blocked)
                except ValueError:
                    pass

        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def _clear_dependency(self, completed_id: int) -> None:
        """完成任务时，从所有其他任务的 blockedBy 中移除。"""
        for f in self.dir.glob("task_*.json"):
            try:
                task = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def list_all(self) -> str:
        tasks = self._all_tasks()
        if not tasks:
            return "暂无任务。"
        lines = []
        for t in tasks:
            marker = {
                "pending": "[ ]", "in_progress": "[>]",
                "completed": "[x]", "deleted": "[-]",
            }.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            wt = f" wt={t['worktree']}" if t.get("worktree") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{wt}{blocked}")

        active = [t for t in tasks if t["status"] not in ("completed", "deleted")]
        done = [t for t in tasks if t["status"] == "completed"]
        lines.append(f"\n({len(done)} 已完成 / {len(active)} 活跃 / 共 {len(tasks)})")
        return "\n".join(lines)

    def render_for_prompt(self) -> str:
        """生成注入 System Prompt 的任务摘要。"""
        tasks = self._all_tasks()
        active = [t for t in tasks if t["status"] not in ("completed", "deleted")]
        if not active:
            return ""
        lines = ["# 持久任务图（跨会话）"]
        for t in active:
            marker = "[>]" if t["status"] == "in_progress" else "[ ]"
            blocked = f" ← blocked by {t['blockedBy']}" if t.get("blockedBy") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
        return "\n".join(lines)


# ── 全局单例 ──

TASK_MGR = TaskManager()


# ── 注册为工具 ──

@tool(
    name="task_create",
    description="创建一个持久任务（跨会话存活），用于大型多阶段工作",
    parallel=False,
)
def task_create(subject: str, description: str = "") -> str:
    return TASK_MGR.create(subject, description)


@tool(
    name="task_update",
    description="更新持久任务的状态或依赖关系",
    parallel=False,
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "任务 ID"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "deleted"],
                "description": "新状态",
            },
            "addBlockedBy": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "添加前置依赖任务 ID",
            },
            "addBlocks": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "添加后续被阻塞任务 ID",
            },
        },
        "required": ["task_id"],
    },
)
def task_update(
    task_id: int,
    status: str = "",
    addBlockedBy: list[int] | None = None,
    addBlocks: list[int] | None = None,
) -> str:
    return TASK_MGR.update(task_id, status, addBlockedBy, addBlocks)


@tool(name="task_list", description="列出所有持久任务及状态摘要")
def task_list() -> str:
    return TASK_MGR.list_all()


@tool(name="task_get", description="按 ID 获取任务详情")
def task_get(task_id: int) -> str:
    return TASK_MGR.get(task_id)
