# -*- coding: utf-8 -*-
"""
会话内短期记忆（预留接口）。

当前未被主流程使用，保留用于后续集成：
  - 追踪最近读取的文件路径
  - 缓存工具结果摘要
  - 可注入 prompt 的动态段提供近期上下文

TODO: 在 Agent._process_tool_calls 中调用 track_file / add_summary，
      并在 prompt.py 动态段中调用 render()。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ShortTermMemory:
    """单次会话内的临时状态，LRU 式保留最近 N 条。"""

    recent_files: list[str] = field(default_factory=list)
    tool_summaries: list[str] = field(default_factory=list)

    MAX_FILES = 10
    MAX_SUMMARIES = 20

    def track_file(self, path: str):
        """记录读取过的文件路径（去重，保留最近 N 个）。"""
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.append(path)
        if len(self.recent_files) > self.MAX_FILES:
            self.recent_files[:] = self.recent_files[-self.MAX_FILES:]

    def add_summary(self, summary: str):
        """缓存工具结果摘要。"""
        self.tool_summaries.append(summary)
        if len(self.tool_summaries) > self.MAX_SUMMARIES:
            self.tool_summaries[:] = self.tool_summaries[-self.MAX_SUMMARIES:]

    def render(self) -> str:
        """生成可注入 prompt 的摘要文本。"""
        parts = []
        if self.recent_files:
            parts.append("近期读取文件：" + ", ".join(self.recent_files[-5:]))
        return "\n".join(parts)
