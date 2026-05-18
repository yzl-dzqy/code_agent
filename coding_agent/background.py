# -*- coding: utf-8 -*-
"""
后台运行槽位：在线程中执行耗时命令，不阻塞主循环。

与持久任务图（tasks.py）的区别：
  - task：描述「要完成什么」，跨会话持久化，带依赖图
  - background：描述「谁在跑、跑到哪」，运行时槽位，进程结束即消亡

工作流：
  1. 模型调用 background_run("make test") → 立即返回 slot_id
  2. 后台线程执行命令，完成后推入通知队列
  3. 每轮 LLM 调用前，agent 排空队列，将结果注入上下文
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import CFG
from .tools.registry import tool
from .utils.log import log

STALL_THRESHOLD_S = 120  # 超过此秒数视为停滞


class NotificationQueue:
    """优先级通知队列，支持同 key 折叠（新消息替换旧消息）。"""

    PRIORITIES = {"immediate": 0, "high": 1, "medium": 2, "low": 3}

    def __init__(self):
        self._queue: list[tuple[int, str | None, str]] = []
        self._lock = threading.Lock()

    def push(self, message: str, priority: str = "medium", key: str | None = None):
        with self._lock:
            if key:
                self._queue = [(p, k, m) for p, k, m in self._queue if k != key]
            self._queue.append((self.PRIORITIES.get(priority, 2), key, message))
            self._queue.sort(key=lambda x: x[0])

    def drain(self) -> list[str]:
        """返回所有待处理消息（按优先级排序）并清空队列。"""
        with self._lock:
            msgs = [m for _, _, m in self._queue]
            self._queue.clear()
            return msgs


class BackgroundManager:
    """后台运行槽位管理器。"""

    def __init__(self):
        self.runtime_dir = CFG.resolved_output_dir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.slots: dict[str, dict] = {}
        self.notifications = NotificationQueue()
        self._lock = threading.Lock()

    def run(self, command: str, timeout: int = 300) -> str:
        """启动后台命令，立即返回 slot_id。"""
        slot_id = uuid.uuid4().hex[:8]
        output_file = self.runtime_dir / f"{slot_id}.log"

        self.slots[slot_id] = {
            "id": slot_id,
            "command": command,
            "status": "running",
            "started_at": time.time(),
            "finished_at": None,
            "result_preview": "",
            "output_file": str(output_file),
        }
        self._persist(slot_id)

        t = threading.Thread(
            target=self._execute,
            args=(slot_id, command, timeout),
            daemon=True,
        )
        t.start()
        log("INFO", "bg_started", f"{slot_id}: {command[:80]}")
        return f"后台任务 {slot_id} 已启动: {command[:80]}\n输出文件: {output_file}"

    def _execute(self, slot_id: str, command: str, timeout: int):
        output_file = Path(self.slots[slot_id]["output_file"])
        try:
            r = subprocess.run(
                command, shell=True, cwd=str(CFG.workdir),
                capture_output=True, text=True, timeout=timeout,
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed" if r.returncode == 0 else "error"
        except subprocess.TimeoutExpired:
            output = f"Error: Timeout ({timeout}s)"
            status = "timeout"
        except Exception as e:
            output = f"Error: {e}"
            status = "error"

        final = output or "(no output)"
        preview = " ".join(final.split())[:500]

        output_file.write_text(final, encoding="utf-8")

        with self._lock:
            slot = self.slots[slot_id]
            slot["status"] = status
            slot["finished_at"] = time.time()
            slot["result_preview"] = preview
        self._persist(slot_id)

        # 推入通知队列
        elapsed = time.time() - self.slots[slot_id]["started_at"]
        self.notifications.push(
            f"[bg:{slot_id}] {status}: {preview[:200]} ({elapsed:.1f}s)",
            priority="high",
            key=slot_id,
        )
        log("INFO", "bg_finished", f"{slot_id}: {status} ({elapsed:.1f}s)")

    def check(self, slot_id: str = "") -> str:
        """查询单个或全部后台任务状态。"""
        if slot_id:
            s = self.slots.get(slot_id)
            if not s:
                return f"Error: 未知槽位 '{slot_id}'"
            elapsed = (s.get("finished_at") or time.time()) - s["started_at"]
            return (
                f"ID: {s['id']}\n状态: {s['status']}\n"
                f"命令: {s['command']}\n耗时: {elapsed:.1f}s\n"
                f"预览: {s['result_preview'][:300]}\n"
                f"输出: {s['output_file']}"
            )
        if not self.slots:
            return "暂无后台任务。"
        lines = []
        for sid, s in self.slots.items():
            elapsed = (s.get("finished_at") or time.time()) - s["started_at"]
            lines.append(f"  {sid} [{s['status']}] {s['command'][:50]} ({elapsed:.0f}s)")
        return "\n".join(lines)

    def drain(self) -> list[str]:
        """排空通知队列，返回所有完成消息。"""
        return self.notifications.drain()

    def stalled_slots(self) -> list[str]:
        """返回运行时间超过阈值的槽位 ID。"""
        now = time.time()
        return [
            sid for sid, s in self.slots.items()
            if s["status"] == "running"
            and (now - s["started_at"]) > STALL_THRESHOLD_S
        ]

    def _persist(self, slot_id: str):
        record = dict(self.slots[slot_id])
        p = self.runtime_dir / f"{slot_id}.json"
        p.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


# ── 全局单例 ──

BG_MGR = BackgroundManager()


# ── 注册为工具 ──

@tool(
    name="background_run",
    description="在后台线程中运行耗时命令（如编译、测试），立即返回 slot_id，不阻塞主循环",
    parallel=False,
)
def background_run(command: str, timeout: int = 300) -> str:
    return BG_MGR.run(command, timeout)


@tool(
    name="check_background",
    description="查询后台任务状态。省略 slot_id 则列出全部",
)
def check_background(slot_id: str = "") -> str:
    return BG_MGR.check(slot_id)
