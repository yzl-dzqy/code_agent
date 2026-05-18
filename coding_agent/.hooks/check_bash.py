#!/usr/bin/env python3
"""
PreToolUse hook 示例：拦截危险的 bash 命令。

退出码约定:
  0 - 放行
  1 - 拦截（stderr 输出拦截原因）
  2 - 放行但注入提示消息（stderr 输出注入内容）
"""
import json
import os
import sys

DANGEROUS = ["rm -rf /", "sudo rm", "shutdown", "reboot", "mkfs", "> /dev/"]

tool_input = json.loads(os.environ.get("HOOK_TOOL_INPUT", "{}"))
command = tool_input.get("command", "")

for pattern in DANGEROUS:
    if pattern in command:
        print(f"危险命令被拦截: {command}", file=sys.stderr)
        sys.exit(1)

# 对 rm 命令发出警告（exit 2 = 注入提示但不拦截）
if "rm " in command:
    print(f"⚠️ 注意: 该命令包含 rm 操作，请确认输出正确", file=sys.stderr)
    sys.exit(2)

sys.exit(0)
