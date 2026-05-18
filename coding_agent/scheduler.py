# -*- coding: utf-8 -*-
"""
定时调度器：Agent 自行安排未来工作。

与持久任务图 / 后台槽位的区别：
  - task：描述「要完成什么」，跨会话持久化，带依赖图
  - background：描述「谁在跑、跑到哪里」，运行时线程槽位
  - scheduler：描述「什么时候做」，用 cron 表达式触发 prompt 注入

两种持久模式：
  - session-only：仅内存，退出即丢
  - durable：持久化到 .agent/scheduled_tasks.json

两种触发模式：
  - recurring：重复触发（7 天自动过期）
  - one-shot：触发一次后自动删除

架构：
  后台线程每秒检查一次 cron 表达式 → 匹配时推入通知队列
  → agent 主循环每轮 LLM 调用前排空队列、注入上下文
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty, Queue

from .config import CFG
from .tools.registry import tool
from .utils.log import log

AUTO_EXPIRY_DAYS = 7
# 整点/半点容易撞车，给 recurring 加随机偏移
JITTER_MINUTES = [0, 30]
JITTER_OFFSET_MAX = 4


# ── Cron 表达式匹配 ──

def cron_matches(expr: str, dt: datetime) -> bool:
    """
    判断 5 字段 cron 表达式是否匹配给定时间。
    字段：minute hour day-of-month month day-of-week
    支持：* (任意)、*/N (步长)、N (精确)、N-M (范围)、N,M (列表)
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    # Python weekday: 0=Mon; cron: 0=Sun → 转换
    cron_dow = (dt.weekday() + 1) % 7
    values = [dt.minute, dt.hour, dt.day, dt.month, cron_dow]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    return all(
        _field_matches(f, v, lo, hi)
        for f, v, (lo, hi) in zip(fields, values, ranges)
    )


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """
    单字段 cron 匹配：支持 *、步长 */N、精确值、范围 a-b、列表 a,b,c。
    步长在 * 与范围上均表示「从起点起每隔 step」能否落到 value。
    """
    if field == "*":
        return True
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
        if part == "*":
            if (value - lo) % step == 0:
                return True
        elif "-" in part:
            start, end = map(int, part.split("-", 1))
            if start <= value <= end and (value - start) % step == 0:
                return True
        else:
            if int(part) == value:
                return True
    return False


# ── PID 锁：防止多会话重复触发 ──

class CronLock:
    """基于 PID 文件的轻量锁，防止多会话同时触发相同定时任务。"""

    def __init__(self, lock_path: Path | None = None):
        self._path = lock_path or (CFG.resolved_output_dir / "cron.lock")

    def acquire(self) -> bool:
        if self._path.exists():
            try:
                stored = int(self._path.read_text().strip())
                os.kill(stored, 0)  # 进程是否存活
                return False  # 锁被占用
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                pass  # 过期锁
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(str(os.getpid()))
        return True

    def release(self) -> None:
        try:
            if self._path.exists():
                if int(self._path.read_text().strip()) == os.getpid():
                    self._path.unlink()
        except (ValueError, OSError):
            pass


# ── 调度器 ──

class CronScheduler:
    """定时调度管理器，后台线程 + 通知队列。"""

    def __init__(self):
        self._tasks_file = CFG.resolved_output_dir / "scheduled_tasks.json"
        self.tasks: list[dict] = []
        self.queue: Queue[str] = Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_minute = -1
        self._lock = CronLock()

    # ── 生命周期 ──

    def start(self) -> None:
        """加载持久任务并启动后台检查线程。"""
        self._load_durable()
        if not self._lock.acquire():
            log("WARN", "cron_lock_held", "另一个会话持有 cron 锁，本会话不触发定时任务")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        if self.tasks:
            log("INFO", "cron_loaded", f"{len(self.tasks)} scheduled tasks")

    def stop(self) -> None:
        """停止后台检查线程并释放 cron 锁。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._lock.release()

    # ── CRUD ──

    def create(
        self,
        cron_expr: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = False,
    ) -> str:
        """创建定时任务。recurring=重复触发，durable=持久化到磁盘。"""
        if len(cron_expr.strip().split()) != 5:
            return "Error: cron 表达式必须为 5 个字段 (min hour dom month dow)"
        task_id = uuid.uuid4().hex[:8]
        task = {
            "id": task_id,
            "cron": cron_expr,
            "prompt": prompt,
            "recurring": recurring,
            "durable": durable,
            "createdAt": time.time(),
        }
        if recurring:
            task["jitter_offset"] = self._jitter(cron_expr)
        self.tasks.append(task)
        if durable:
            self._save_durable()
        mode = "recurring" if recurring else "one-shot"
        store = "durable" if durable else "session-only"
        log("INFO", "cron_created", f"{task_id} [{mode}/{store}] {cron_expr}")
        return f"已创建定时任务 {task_id} ({mode}, {store}): cron={cron_expr}\n提示: {prompt[:80]}"

    def delete(self, task_id: str) -> str:
        """按 ID 删除定时任务，同步更新磁盘。"""
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t["id"] != task_id]
        if len(self.tasks) < before:
            self._save_durable()
            log("INFO", "cron_deleted", task_id)
            return f"已删除定时任务 {task_id}"
        return f"Error: 未找到任务 {task_id}"

    def list_tasks(self) -> str:
        """列出所有定时任务的摘要信息。"""
        if not self.tasks:
            return "暂无定时任务。"
        lines = []
        for t in self.tasks:
            mode = "recurring" if t["recurring"] else "one-shot"
            store = "durable" if t["durable"] else "session"
            age_h = (time.time() - t["createdAt"]) / 3600
            lines.append(
                f"  {t['id']}  {t['cron']}  [{mode}/{store}] "
                f"({age_h:.1f}h): {t['prompt'][:60]}"
            )
        return "\n".join(lines)

    # ── 通知排空 ──

    def drain(self) -> list[str]:
        """排空通知队列，返回所有已触发的提示。"""
        msgs: list[str] = []
        while True:
            try:
                msgs.append(self.queue.get_nowait())
            except Empty:
                break
        return msgs

    # ── 错过任务检测 ──

    def detect_missed(self) -> list[dict]:
        """启动时检测在离线期间本应触发但错过的持久任务（最多回溯 24h）。"""
        now = datetime.now()
        missed = []
        for t in self.tasks:
            last = t.get("last_fired")
            if last is None:
                continue
            check = datetime.fromtimestamp(last) + timedelta(minutes=1)
            cap = min(now, check + timedelta(hours=24))
            while check <= cap:
                if cron_matches(t["cron"], check):
                    missed.append({
                        "id": t["id"],
                        "cron": t["cron"],
                        "prompt": t["prompt"],
                        "missed_at": check.isoformat(),
                    })
                    break
                check += timedelta(minutes=1)
        return missed

    # ── 内部 ──

    def _loop(self) -> None:
        """后台线程主循环，每秒检查一次，每分钟仅触发一次 _tick。"""
        while not self._stop.is_set():
            now = datetime.now()
            cur_min = now.hour * 60 + now.minute
            if cur_min != self._last_minute:
                self._last_minute = cur_min
                self._tick(now)
            self._stop.wait(timeout=1)

    def _tick(self, now: datetime) -> None:
        """单次 tick：检查所有任务的 cron 匹配，清理过期和 one-shot。"""
        expired, oneshots = [], []
        for t in self.tasks:
            # 自动过期
            age_days = (time.time() - t["createdAt"]) / 86400
            if t["recurring"] and age_days > AUTO_EXPIRY_DAYS:
                expired.append(t["id"])
                continue
            check = now
            jitter = t.get("jitter_offset", 0)
            if jitter:
                # 对整点/半点类任务人为前移「虚拟时刻」，使触发分散到不同分钟，避免多任务同刻入队
                check = now - timedelta(minutes=jitter)
            if cron_matches(t["cron"], check):
                self.queue.put(f"[定时任务 {t['id']}]: {t['prompt']}")
                t["last_fired"] = time.time()
                log("INFO", "cron_fired", t["id"])
                if not t["recurring"]:
                    oneshots.append(t["id"])

        remove = set(expired) | set(oneshots)
        if remove:
            self.tasks = [t for t in self.tasks if t["id"] not in remove]
            for tid in expired:
                log("INFO", "cron_expired", tid)
            self._save_durable()

    def _jitter(self, cron_expr: str) -> int:
        """为整点/半点 cron 计算确定性偏移量，避免多任务同时触发。"""
        fields = cron_expr.strip().split()
        try:
            if int(fields[0]) in JITTER_MINUTES:
                return (hash(cron_expr) % JITTER_OFFSET_MAX) + 1
        except ValueError:
            pass
        return 0

    def _load_durable(self) -> None:
        """从 scheduled_tasks.json 恢复持久任务。"""
        if not self._tasks_file.exists():
            return
        try:
            data = json.loads(self._tasks_file.read_text(encoding="utf-8"))
            self.tasks = [t for t in data if t.get("durable")]
        except Exception as e:
            log("WARN", "cron_load_error", str(e))

    def _save_durable(self) -> None:
        """将 durable 任务持久化到 scheduled_tasks.json。"""
        durable = [t for t in self.tasks if t.get("durable")]
        self._tasks_file.parent.mkdir(parents=True, exist_ok=True)
        self._tasks_file.write_text(
            json.dumps(durable, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


# ── 全局单例 ──

SCHEDULER = CronScheduler()


# ── 注册为工具 ──

@tool(
    name="cron_create",
    description=(
        "创建定时任务。cron 为 5 字段表达式 (min hour dom month dow)，"
        "到时间后 prompt 会自动注入对话。"
        "recurring=true 重复触发(7天过期)，false=触发一次后删除。"
        "durable=true 持久化到磁盘（跨会话保留）。"
    ),
)
def cron_create(
    cron: str,
    prompt: str,
    recurring: bool = True,
    durable: bool = False,
) -> str:
    return SCHEDULER.create(cron, prompt, recurring, durable)


@tool(name="cron_delete", description="删除定时任务")
def cron_delete(task_id: str) -> str:
    return SCHEDULER.delete(task_id)


@tool(name="cron_list", description="列出所有定时任务")
def cron_list() -> str:
    return SCHEDULER.list_tasks()
