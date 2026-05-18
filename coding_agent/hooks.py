# -*- coding: utf-8 -*-
"""
Hook 系统：在 Agent 主循环外注入扩展行为。

支持三个事件：
  - SessionStart: 会话启动时触发
  - PreToolUse:   工具执行前触发（可拦截/修改参数）
  - PostToolUse:  工具执行后触发（可注入附加信息）

退出码约定：
  - 0: 继续（stdout 若为 JSON 可携带 updatedInput / additionalContext）
  - 1: 拦截（stderr 作为拦截原因）
  - 2: 注入消息（stderr 作为注入内容）

配置文件：仅使用 coding_agent/.hooks/.hooks.json（严格模式）
信任标记：仅使用 coding_agent/.agent/.agent_trusted（或 sdk_mode=True）
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .config import CFG

from .utils.log import log

HOOK_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse")


@dataclass
class HookResult:
    """单次事件的 hook 聚合结果。"""
    blocked: bool = False
    block_reason: str = ""
    messages: list[str] = field(default_factory=list)
    # PreToolUse 可能修改工具输入
    updated_input: dict[str, Any] | None = None


class HookManager:
    """
    从 .hooks.json 加载并执行 hook 命令。

    .hooks.json 示例:
    {
      "hooks": {
        "PreToolUse": [
          {"matcher": "bash", "command": "python .hooks/check_bash.py"},
          {"matcher": "*",    "command": "echo ok"}
        ],
        "PostToolUse": [
          {"matcher": "*", "command": "python .hooks/audit.py"}
        ],
        "SessionStart": [
          {"command": "echo 'session started'"}
        ]
      }
    }
    """

    def __init__(
        self,
        config_path: Path | None = None,
        *,
        sdk_mode: bool = False,
        timeout: int = 30,
    ):
        self.timeout = timeout
        self._sdk_mode = sdk_mode
        self._trust_marker = CFG.resolved_output_dir / ".agent_trusted"
        self._config_path: Path | None = None
        self.hooks: dict[str, list[dict]] = {e: [] for e in HOOK_EVENTS}
        self._loaded = False

        # 严格模式：仅允许固定路径（coding_agent/.hooks/.hooks.json）。
        config_path = config_path or (CFG.pkg_dir / ".hooks" / ".hooks.json")
        self._config_path = config_path
        if config_path is not None and config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                for event in HOOK_EVENTS:
                    self.hooks[event] = cfg.get("hooks", {}).get(event, [])
                self._loaded = True
                total = sum(len(v) for v in self.hooks.values())
                log("INFO", "hooks_loaded", f"{config_path} ({total} hooks)")
            except Exception as e:
                log("WARN", "hooks_config_error", str(e))

    @property
    def is_active(self) -> bool:
        return self._loaded and self._is_trusted()

    @property
    def hook_count(self) -> int:
        return sum(len(v) for v in self.hooks.values())

    def summary(self) -> str:
        """返回可读的 hook 状态摘要。"""
        if not self._loaded:
            return "未加载（无 hooks 配置）"
        parts = []
        for event in HOOK_EVENTS:
            n = len(self.hooks[event])
            if n:
                parts.append(f"{event}:{n}")
        trusted = "已信任" if self._is_trusted() else "未信任"
        src = str(self._config_path) if self._config_path else "unknown"
        return f"{', '.join(parts) or '无 hook'}  [{trusted}]  ({src})"

    def _is_trusted(self) -> bool:
        if self._sdk_mode:
            return True
        return self._trust_marker.exists()

    def run_hooks(
        self,
        event: str,
        context: dict[str, Any] | None = None,
    ) -> HookResult:
        """执行指定事件的所有 hook，返回聚合结果。"""
        result = HookResult()

        if not self._is_trusted():
            return result

        hooks = self.hooks.get(event, [])
        if not hooks:
            return result

        ctx = context or {}

        for hook_def in hooks:
            # matcher 过滤（仅 PreToolUse / PostToolUse）
            matcher = hook_def.get("matcher")
            if matcher and ctx:
                tool_name = ctx.get("tool_name", "")
                if matcher != "*" and matcher != tool_name:
                    continue

            command = hook_def.get("command", "")
            if not command:
                continue

            # 构建环境变量传递上下文（截断 10000 字符，避免超大 JSON 撑爆环境）
            env = dict(os.environ)
            env["HOOK_EVENT"] = event
            env["HOOK_TOOL_NAME"] = ctx.get("tool_name", "")
            env["HOOK_TOOL_INPUT"] = json.dumps(
                ctx.get("tool_input", {}), ensure_ascii=False
            )[:10000]
            if "tool_output" in ctx:
                env["HOOK_TOOL_OUTPUT"] = str(ctx["tool_output"])[:10000]

            try:
                # hooks 命令在 coding_agent 根目录执行，
                # 这样配置中的相对路径（如 python .hooks/check_bash.py）可直接命中。
                run_cwd = str(CFG.pkg_dir)
                r = subprocess.run(
                    command,
                    shell=True,
                    cwd=run_cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )

                if r.returncode == 0:
                    # 0：继续；stdout 可为 JSON（updatedInput / additionalContext）
                    if r.stdout.strip():
                        log("DEBUG", f"hook:{event}", r.stdout.strip()[:100])
                    try:
                        out = json.loads(r.stdout)
                        if "updatedInput" in out and ctx:
                            ctx["tool_input"] = out["updatedInput"]
                            result.updated_input = out["updatedInput"]
                        if "additionalContext" in out:
                            result.messages.append(out["additionalContext"])
                    except (json.JSONDecodeError, TypeError):
                        pass

                elif r.returncode == 1:
                    # 1：拦截工具，stderr 为原因
                    reason = r.stderr.strip() or "Blocked by hook"
                    result.blocked = True
                    result.block_reason = reason
                    log("WARN", f"hook:{event}", f"BLOCKED: {reason[:200]}")
                    break  # 被拦截后不再执行后续 hook

                elif r.returncode == 2:
                    # 2：向对话注入额外内容，正文在 stderr
                    msg = r.stderr.strip()
                    if msg:
                        result.messages.append(msg)
                        log("INFO", f"hook:{event}", f"INJECT: {msg[:200]}")

            except subprocess.TimeoutExpired:
                log("WARN", f"hook:{event}", f"Timeout ({self.timeout}s)")
            except Exception as e:
                log("WARN", f"hook:{event}", f"Error: {e}")

        return result
