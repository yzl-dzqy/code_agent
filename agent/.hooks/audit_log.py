#!/usr/bin/env python3
"""
PostToolUse hook 示例：记录所有工具调用到审计日志。

所有工具执行后触发，将调用信息追加到 .agent/audit.jsonl。
"""
import json
import os
from datetime import datetime
from pathlib import Path

event = os.environ.get("HOOK_EVENT", "")
tool_name = os.environ.get("HOOK_TOOL_NAME", "")
tool_input = os.environ.get("HOOK_TOOL_INPUT", "{}")
tool_output = os.environ.get("HOOK_TOOL_OUTPUT", "")[:500]

log_dir = Path(".agent")
log_dir.mkdir(exist_ok=True)

record = {
    "time": datetime.now().isoformat(),
    "tool": tool_name,
    "input": json.loads(tool_input),
    "output_preview": tool_output,
}

with open(log_dir / "audit.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(record, ensure_ascii=False) + "\n")
