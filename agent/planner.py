# -*- coding: utf-8 -*-
"""计划/待办管理系统。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import CFG
from .tools.registry import tool


@dataclass
class PlanItem:
    content: str
    status: str  # pending / in_progress / completed
    active_form: str = ""


@dataclass
class PlanState:
    items: list[PlanItem] = field(default_factory=list)
    rounds_since_update: int = 0


class Planner:
    """会话级待办管理。"""

    def __init__(self):
        self.state = PlanState()
        self._load()

    def _save(self) -> None:
        """持久化当前状态到 todo.json。"""
        CFG.resolved_output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "items": [{"content": i.content, "status": i.status, "active_form": i.active_form} for i in self.state.items],
            "rounds_since_update": self.state.rounds_since_update,
        }
        CFG.todo_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> None:
        """从 todo.json 恢复状态。文件不存在或损坏时静默跳过。"""
        if not CFG.todo_file.exists():
            return
        try:
            data = json.loads(CFG.todo_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.state.items = [
            PlanItem(content=str(r.get("content", "")), status=str(r.get("status", "pending")),
                     active_form=str(r.get("active_form", "")))
            for r in (data.get("items") or []) if isinstance(r, dict)
        ]
        self.state.rounds_since_update = int(data.get("rounds_since_update", 0))

    def update(self, items: list[dict[str, Any]]) -> str:
        """
        整表替换当前待办。

        校验规则：最多 12 条，in_progress 最多 1 条。
        全部 completed 时自动归档并清空。
        """
        if len(items) > 12:
            raise ValueError("最多 12 条")
        normalized = []
        ip_count = 0
        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                raise ValueError(f"Item {i}: 须为对象")
            content = str(raw.get("content", "")).strip()
            status = str(raw.get("status", "pending")).lower()
            active_form = str(raw.get("activeForm", "")).strip()
            if not content:
                raise ValueError(f"Item {i}: content 必填")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {i}: 无效 status '{status}'")
            if status == "in_progress":
                ip_count += 1
            normalized.append(PlanItem(content=content, status=status, active_form=active_form))
        if ip_count > 1:
            raise ValueError("in_progress 最多一条")
        self.state.items = normalized
        self.state.rounds_since_update = 0

        if normalized and all(i.status == "completed" for i in normalized):
            arc = self._archive()
            self._clear()
            return f"全部完成，已归档: {arc.relative_to(CFG.workdir)}"
        self._save()
        return self.render()

    def _archive(self) -> Path:
        """将已完成的待办归档到 todo_archive/ 目录，返回归档文件路径。"""
        CFG.todo_archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = CFG.todo_archive_dir / f"todo_done_{stamp}.json"
        payload = {
            "archived_at": datetime.now().isoformat(timespec="seconds"),
            "items": [{"content": i.content, "status": i.status} for i in self.state.items],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _clear(self) -> None:
        """重置内存状态并写入空的 todo.json。"""
        self.state.items = []
        self.state.rounds_since_update = 0
        CFG.resolved_output_dir.mkdir(parents=True, exist_ok=True)
        CFG.todo_file.write_text('{"items":[],"rounds_since_update":0}', encoding="utf-8")

    def note_round_without_update(self) -> None:
        """记录一轮未调用 todo 工具，用于触发提醒。"""
        self.state.rounds_since_update += 1
        self._save()

    def reminder(self) -> str | None:
        """若存在待办且多轮未调用 todo，返回提醒文案；否则 None。"""
        if not self.state.items:
            return None
        if self.state.rounds_since_update < CFG.plan_reminder_interval:
            return None
        return (
            "[系统提醒] 已连续多轮未更新待办。请立即调用 todo 工具：将已完成条目标为 completed。"
            "todo 是整表替换，必须传入全部条目。"
        )

    def render(self) -> str:
        """渲染待办看板为可读文本，包含进度统计。"""
        if not self.state.items:
            return "尚无计划。"
        lines = []
        for it in self.state.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[it.status]
            line = f"{marker} {it.content}"
            if it.status == "in_progress" and it.active_form:
                line += f" ({it.active_form})"
            lines.append(line)
        c = sum(1 for i in self.state.items if i.status == "completed")
        lines.append(f"\n({c}/{len(self.state.items)} 已完成)")
        return "\n".join(lines)

    def counts(self) -> tuple[int, int, int, int]:
        """返回 (completed, in_progress, pending, total) 四元组。"""
        items = self.state.items
        c = sum(1 for i in items if i.status == "completed")
        ip = sum(1 for i in items if i.status == "in_progress")
        pe = sum(1 for i in items if i.status == "pending")
        return c, ip, pe, len(items)

    def progress_bar(self, width: int = 20) -> tuple[str, int]:
        """生成进度条字符串和完成百分比。"""
        _, _, _, total = self.counts()
        if total == 0:
            return "░" * width, 0
        done = sum(1 for i in self.state.items if i.status == "completed")
        pct = int(100 * done / total)
        filled = min(width, max(0, round(width * done / total)))
        return "█" * filled + "░" * (width - filled), pct


# 全局单例
PLANNER = Planner()


# 注册为工具
@tool(
    name="todo",
    description=(
        "重写当前待办计划（整表替换）。每完成一步立即标为 completed，"
        "开始某步标为 in_progress。必须传入全部条目。"
    ),
    parallel=False,
    parameters={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        "activeForm": {"type": "string", "description": "可选：进行中条目描述"},
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["items"],
    },
)
def todo_tool(items: list[dict[str, Any]]) -> str:
    return PLANNER.update(items)
