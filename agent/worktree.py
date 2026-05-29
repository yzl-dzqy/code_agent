# -*- coding: utf-8 -*-
"""
Git Worktree 隔离执行平面。

核心思想：任务是控制面，worktree 是执行面。
  - task 描述「要完成什么」（控制面）
  - worktree 描述「在哪里跑」（执行面）
  - 两者通过 task_id 关联，但彼此独立

架构：
  .agent/worktrees/
    index.json          worktree 注册表
    events.jsonl        生命周期事件日志（追加写入）
  .agent/worktrees/<name>/   各 worktree 的实际目录

生命周期：
  create → enter → run (多次) → closeout(keep|remove)
    ↓        ↓       ↓              ↓
  EventBus 全程记录可观测事件

与其他子系统的边界：
  - background.py  线程槽位，进程级隔离
  - worktree.py    目录级隔离，git 分支级隔离
  - tasks.py       只管"做什么"，不管"在哪做"
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .config import CFG
from .tools.registry import tool
from .utils.log import log


# ── EventBus：追加写入的生命周期事件日志 ──

class EventBus:
    """
    Worktree 生命周期事件日志。

    所有 worktree 操作（create/enter/run/closeout）都会产生事件，
    追加写入 events.jsonl，供调试和审计。
    """

    def __init__(self, log_path: Path):
        self.path = log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("")

    def emit(self, event: str, **payload) -> None:
        """追加一条 JSON 行到 events.jsonl（含时间戳）。"""
        record = {"event": event, "ts": time.time()}
        record.update(payload)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def recent(self, limit: int = 20) -> str:
        """读取最近 N 条事件。"""
        n = max(1, min(limit, 200))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        items = []
        for line in lines[-n:]:
            try:
                items.append(json.loads(line))
            except Exception:
                items.append({"event": "parse_error", "raw": line})
        return json.dumps(items, indent=2, ensure_ascii=False)


# ── WorktreeManager：git worktree 的 CRUD + 命令执行 ──

def _detect_repo_root() -> Path | None:
    """检测当前工作目录所在的 git 仓库根目录。"""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(CFG.workdir), capture_output=True, text=True, timeout=10,
        )
        root = Path(r.stdout.strip())
        return root if r.returncode == 0 and root.exists() else None
    except Exception:
        return None


class WorktreeManager:
    """
    Git Worktree 隔离执行管理器。

    每个 worktree 是一个独立的目录 + git 分支，可绑定到一个 task。
    支持在 worktree 内执行命令，实现目录级隔离。
    """

    def __init__(self):
        self.repo_root = _detect_repo_root() or CFG.workdir
        self.base_dir = CFG.resolved_output_dir / "worktrees"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.base_dir / "index.json"
        self.events = EventBus(self.base_dir / "events.jsonl")
        self.git_available = self._check_git()

        if not self.index_path.exists():
            self._save_index({"worktrees": []})

    def _check_git(self) -> bool:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(self.repo_root), capture_output=True,
                text=True, timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _git(self, args: list[str]) -> str:
        """在 repo_root 下执行 git 命令。"""
        if not self.git_available:
            raise RuntimeError("当前目录不是 git 仓库，无法使用 worktree 功能")
        r = subprocess.run(
            ["git", *args], cwd=str(self.repo_root),
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            raise RuntimeError(
                (r.stdout + r.stderr).strip()
                or f"git {' '.join(args)} 失败")
        return (r.stdout + r.stderr).strip() or "(no output)"

    # ── 索引操作 ──

    def _load_index(self) -> dict:
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _save_index(self, data: dict) -> None:
        """持久化 worktree 注册表到 index.json。"""
        self.index_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _find(self, name: str) -> dict | None:
        for wt in self._load_index().get("worktrees", []):
            if wt.get("name") == name:
                return wt
        return None

    def _update_entry(self, name: str, **changes) -> dict:
        idx = self._load_index()
        updated = None
        for item in idx.get("worktrees", []):
            if item.get("name") == name:
                item.update(changes)
                updated = item
                break
        self._save_index(idx)
        if not updated:
            raise ValueError(f"Worktree '{name}' 不存在")
        return updated

    @staticmethod
    def _validate_name(name: str) -> None:
        """校验名称仅含安全字符且长度在限制内。"""
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name or ""):
            raise ValueError("名称无效，须 1-40 个字符：字母/数字/._-")

    # ── CRUD ──

    def create(
        self,
        name: str,
        task_id: int | None = None,
        base_ref: str = "HEAD",
    ) -> str:
        """创建 git worktree 并注册到索引。可选绑定到 task。"""
        self._validate_name(name)
        if self._find(name):
            raise ValueError(f"Worktree '{name}' 已存在")

        path = self.base_dir / name
        branch = f"wt/{name}"

        self.events.emit("worktree.create", wt_name=name, task_id=task_id)
        self._git(["worktree", "add", "-b", branch, str(path), base_ref])

        entry = {
            "name": name,
            "path": str(path),
            "branch": branch,
            "task_id": task_id,
            "status": "active",
            "created_at": time.time(),
        }
        idx = self._load_index()
        idx["worktrees"].append(entry)
        self._save_index(idx)

        # 如果绑定了 task，更新 task 的 worktree 字段
        if task_id is not None:
            self._bind_task(task_id, name)

        log("INFO", "worktree_created", f"{name} -> {path}")
        return json.dumps(entry, indent=2, ensure_ascii=False)

    def run_in(self, name: str, command: str) -> str:
        """在指定 worktree 目录中执行 shell 命令。"""
        wt = self._find(name)
        if not wt:
            return f"Error: 未知 worktree '{name}'"
        path = Path(wt["path"])
        if not path.exists():
            return f"Error: worktree 路径不存在: {path}"

        self.events.emit("worktree.run", wt_name=name,
                         command=command[:120])
        try:
            r = subprocess.run(
                command, shell=True, cwd=str(path),
                capture_output=True, text=True, timeout=300,
            )
            return (r.stdout + r.stderr).strip()[:50000] or "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: 超时 (300s)"

    def status(self, name: str) -> str:
        """查看 worktree 的 git 状态。"""
        wt = self._find(name)
        if not wt:
            return f"Error: 未知 worktree '{name}'"
        path = Path(wt["path"])
        if not path.exists():
            return f"Error: worktree 路径不存在: {path}"
        r = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=str(path), capture_output=True, text=True, timeout=60,
        )
        return (r.stdout + r.stderr).strip() or "工作区干净"

    def closeout(
        self,
        name: str,
        action: str,
        reason: str = "",
        force: bool = False,
        complete_task: bool = False,
    ) -> str:
        """
        关闭 worktree 执行通道。

        action:
          - "keep": 保留 worktree 目录，标记为 kept（后续可继续使用）
          - "remove": 删除 worktree 目录和分支
        complete_task: 若为 True 且绑定了 task，同时将 task 标为 completed
        """
        if action not in ("keep", "remove"):
            return "Error: action 必须是 'keep' 或 'remove'"
        wt = self._find(name)
        if not wt:
            return f"Error: 未知 worktree '{name}'"

        task_id = wt.get("task_id")

        if action == "keep":
            self._update_entry(name, status="kept", kept_at=time.time())
            self.events.emit("worktree.kept", wt_name=name, reason=reason)
        else:
            # 删除 git worktree
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(wt["path"])
            try:
                self._git(args)
            except RuntimeError as e:
                return f"Error: 删除失败 — {e}"
            self._update_entry(name, status="removed",
                               removed_at=time.time())
            self.events.emit("worktree.removed", wt_name=name, reason=reason)

        # 关联 task 处理
        if task_id is not None:
            if complete_task:
                self._complete_task(task_id)
            self._unbind_task(task_id)

        log("INFO", "worktree_closeout",
            f"{name}: {action} (task={task_id})")
        return f"Worktree '{name}' 已{action}。" + (
            f" 任务 #{task_id} 已完成。" if complete_task and task_id else "")

    def list_all(self) -> str:
        """列出所有注册的 worktree。"""
        wts = self._load_index().get("worktrees", [])
        if not wts:
            return "暂无 worktree。"
        lines = []
        for wt in wts:
            task = f" task=#{wt['task_id']}" if wt.get("task_id") else ""
            lines.append(
                f"  [{wt.get('status', '?')}] {wt['name']} "
                f"({wt.get('branch', '-')}){task}")
        return "\n".join(lines)

    # ── Task 关联（延迟导入避免循环依赖）──

    @staticmethod
    def _bind_task(task_id: int, wt_name: str):
        from .tasks import TASK_MGR
        task = TASK_MGR._load(task_id)
        task["worktree"] = wt_name
        if task["status"] == "pending":
            task["status"] = "in_progress"
        TASK_MGR._save(task)

    @staticmethod
    def _unbind_task(task_id: int):
        from .tasks import TASK_MGR
        try:
            task = TASK_MGR._load(task_id)
            task.pop("worktree", None)
            TASK_MGR._save(task)
        except ValueError:
            pass

    @staticmethod
    def _complete_task(task_id: int):
        from .tasks import TASK_MGR
        TASK_MGR.update(task_id, status="completed")


# ── 全局单例 ──

WT_MGR = WorktreeManager()


# ── 注册为工具 ──

@tool(
    name="worktree_create",
    description=(
        "创建 git worktree 隔离执行通道。"
        "可选绑定到 task_id，实现「任务→独立目录→独立分支」的隔离执行"
    ),
    parallel=False,
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "worktree 名称 (1-40字符)"},
            "task_id": {"type": "integer", "description": "可选：绑定的任务 ID"},
            "base_ref": {"type": "string", "description": "起始 ref，默认 HEAD"},
        },
        "required": ["name"],
    },
)
def worktree_create(
    name: str, task_id: int | None = None, base_ref: str = "HEAD",
) -> str:
    return WT_MGR.create(name, task_id, base_ref)


@tool(
    name="worktree_run",
    description="在指定 worktree 目录中执行 shell 命令（目录级隔离）",
)
def worktree_run(name: str, command: str) -> str:
    return WT_MGR.run_in(name, command)


@tool(
    name="worktree_status",
    description="查看某个 worktree 的 git 状态",
)
def worktree_status(name: str) -> str:
    return WT_MGR.status(name)


@tool(
    name="worktree_closeout",
    description=(
        "关闭 worktree 通道。action='keep' 保留目录，"
        "action='remove' 删除。complete_task=true 同时完成绑定的任务"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "action": {"type": "string", "enum": ["keep", "remove"]},
            "reason": {"type": "string"},
            "force": {"type": "boolean"},
            "complete_task": {"type": "boolean"},
        },
        "required": ["name", "action"],
    },
)
def worktree_closeout(
    name: str,
    action: str,
    reason: str = "",
    force: bool = False,
    complete_task: bool = False,
) -> str:
    return WT_MGR.closeout(name, action, reason, force, complete_task)


@tool(name="worktree_list", description="列出所有 worktree 及状态")
def worktree_list() -> str:
    return WT_MGR.list_all()


@tool(
    name="worktree_events",
    description="查看 worktree 生命周期事件日志",
)
def worktree_events(limit: int = 20) -> str:
    return WT_MGR.events.recent(limit)
