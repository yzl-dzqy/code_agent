# -*- coding: utf-8 -*-
"""Shell 工具：bash, run_python。"""

from __future__ import annotations

import subprocess
import sys

from ...config import CFG
from ...utils.log import log, preview_text
from ..registry import tool


_DANGEROUS = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]


@tool(name="bash", description="执行 shell 命令", parallel=False)
def bash(command: str) -> str:
    log("INFO", "bash", preview_text(command, 100))
    if any(d in command for d in _DANGEROUS):
        return "Error: 危险命令已拦截"
    try:
        result = subprocess.run(
            command, shell=True, cwd=CFG.workdir,
            capture_output=True, text=True, timeout=CFG.bash_timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Error: 超时 ({CFG.bash_timeout}s)"
    out = (result.stdout + result.stderr).strip() or "(无输出)"
    return out[:100_000]


@tool(name="run_python", description="执行 Python 代码片段", parallel=False)
def run_python(code: str, timeout_sec: int = 30) -> str:
    t = min(max(5, timeout_sec), CFG.python_timeout)
    log("INFO", "run_python", preview_text(code, 100))
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=CFG.workdir, capture_output=True, text=True, timeout=t,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            out = f"[exit {proc.returncode}]\n{out}"
        return out[:100_000] if out.strip() else "(无输出)"
    except subprocess.TimeoutExpired:
        return f"Error: 超时 ({t}s)"
    except Exception as exc:
        return f"Error: {exc}"
