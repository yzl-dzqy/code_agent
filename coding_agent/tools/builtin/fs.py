# -*- coding: utf-8 -*-
"""文件系统工具：read, write, edit, multi_edit, list_dir, grep, glob, patch。"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from ...config import CFG
from ..registry import tool


def _safe_path(path_str: str) -> Path:
    """解析并防止路径逃出工作区。"""
    p = (CFG.workdir / path_str).resolve()
    if not p.is_relative_to(CFG.workdir):
        raise ValueError(f"路径逃出工作区: {path_str}")
    return p


def _persist_large(tid: str, output: str) -> str:
    """超大输出落盘。"""
    if len(output) <= CFG.persist_threshold:
        return output
    CFG.tool_results_dir.mkdir(parents=True, exist_ok=True)
    stored = CFG.tool_results_dir / f"{tid}.txt"
    if not stored.exists():
        stored.write_text(output)
    preview = output[:CFG.persist_preview_chars]
    rel = stored.relative_to(CFG.workdir)
    return f"<persisted-output>\nFull output: {rel}\nPreview:\n{preview}\n</persisted-output>"


@tool(name="read_file", description="读取文件内容")
def read_file(path: str, limit: int = 0) -> str:
    try:
        fp = _safe_path(path)
        content = fp.read_text()
        if path.lower().endswith(".json"):
            try:
                return json.dumps(json.loads(content), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                pass
        lines = content.splitlines()
        if limit and limit > 0 and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines)-limit} more lines)"]
        return "\n".join(lines)
    except Exception as exc:
        return f"Error: {exc}"


@tool(name="write_file", description="将内容写入文件（创建或覆盖）")
def write_file(path: str, content: str) -> str:
    try:
        fp = _safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"已写入 {len(content)} bytes → {path}"
    except Exception as exc:
        return f"Error: {exc}"


@tool(name="edit_file", description="精确替换文件中的一段文本")
def edit_file(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = _safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: 文本未找到 in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"已编辑 {path}"
    except Exception as exc:
        return f"Error: {exc}"


@tool(
    name="multi_edit",
    description="批量替换多个文件中的文本",
    parameters={
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
        },
        "required": ["changes"],
    },
)
def multi_edit(changes: list) -> str:
    if not isinstance(changes, list) or not changes:
        return "Error: changes 必须是非空列表"
    results = []
    for i, c in enumerate(changes):
        if not isinstance(c, dict):
            results.append(f"[{i}] Error: 条目须为对象")
            continue
        r = edit_file(str(c.get("path", "")), str(c.get("old_text", "")), str(c.get("new_text", "")))
        results.append(f"[{i}] {r}")
    return "\n".join(results)


@tool(name="list_dir", description="列出目录下的文件和子目录")
def list_dir(path: str = ".", max_depth: int = 2) -> str:
    try:
        root = _safe_path(path)
        depth_limit = min(max(0, max_depth), CFG.list_dir_max_depth)
        entries: list[str] = []

        def walk(cur: Path, depth: int):
            if len(entries) >= CFG.list_dir_max_entries or depth > depth_limit:
                return
            try:
                kids = sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return
            for child in kids:
                if child.name in CFG.skip_dir_names:
                    continue
                rel = child.relative_to(CFG.workdir)
                indent = "  " * depth
                entries.append(f"{indent}{rel.as_posix()}{'/' if child.is_dir() else ''}")
                if child.is_dir():
                    walk(child, depth + 1)
                if len(entries) >= CFG.list_dir_max_entries:
                    return

        walk(root, 0)
        if len(entries) >= CFG.list_dir_max_entries:
            entries.append(f"... (上限 {CFG.list_dir_max_entries})")
        return "\n".join(entries) if entries else "(空目录)"
    except Exception as exc:
        return f"Error: {exc}"


@tool(name="grep", description="正则搜索文件内容")
def grep(pattern: str, path: str = ".", file_glob: str = "", max_matches: int = 100) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"Error: 无效正则: {exc}"
    cap = min(max(1, max_matches), CFG.grep_max_matches)
    base = _safe_path(path)
    matches: list[str] = []

    def scan(fp: Path):
        if len(matches) >= cap:
            return
        rel = fp.relative_to(CFG.workdir)
        # file_glob 同时匹配相对路径与文件名，便于 "*.py" 或 "src/**/*.ts"
        if file_glob and not (fnmatch.fnmatch(rel.as_posix(), file_glob) or fnmatch.fnmatch(fp.name, file_glob)):
            return
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(matches) >= cap:
                break
            if rx.search(line):
                snippet = line[:800] + ("..." if len(line) > 800 else "")
                matches.append(f"{rel.as_posix()}:{lineno}:{snippet}")

    if base.is_file():
        scan(base)
    else:
        for fp in base.rglob("*"):
            if not fp.is_file():
                continue
            try:
                parts = fp.relative_to(CFG.workdir).parts
            except ValueError:
                continue
            # 路径任一段落在跳过目录（如 .git/node_modules）则整文件跳过
            if CFG.skip_dir_names.intersection(parts):
                continue
            scan(fp)
            if len(matches) >= cap:
                break

    if not matches:
        return "(无匹配)"
    if len(matches) >= cap:
        matches.append(f"... (上限 {cap})")
    return "\n".join(matches)


@tool(name="glob", description="工作区 glob 匹配文件路径")
def glob_files(pattern: str, max_results: int = 200) -> str:
    try:
        norm = pattern.replace("\\", "/")
        if norm.startswith("/") or ".." in norm.split("/"):
            return "Error: 仅允许相对路径"
        cap = min(max(1, max_results), CFG.glob_max_results)
        results = []
        for p in sorted(CFG.workdir.glob(norm)):
            try:
                rp = p.resolve()
                if not rp.is_relative_to(CFG.workdir):
                    continue
                results.append(p.relative_to(CFG.workdir).as_posix())
            except ValueError:
                continue
            if len(results) >= cap:
                break
        return "\n".join(results) if results else "(无匹配)"
    except Exception as exc:
        return f"Error: {exc}"


@tool(name="patch", description="应用 unified diff 补丁", parallel=False)
def patch(patch_content: str) -> str:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False, encoding="utf-8") as f:
            f.write(patch_content)
            tmp_path = f.name
        result = subprocess.run(
            ["patch", "--batch", "-p1", "-i", tmp_path],
            cwd=CFG.workdir, capture_output=True, text=True, timeout=30,
        )
        out = (result.stdout + result.stderr).strip()
        return f"补丁{'成功' if result.returncode == 0 else '失败'}。\n{out}"
    except FileNotFoundError:
        return "Error: 未找到 patch 命令"
    except subprocess.TimeoutExpired:
        return "Error: patch 超时"
    except Exception as exc:
        return f"Error: {exc}"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
