# -*- coding: utf-8 -*-
"""全局配置：Pydantic Settings，从 .env 自动加载。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_env_file() -> str:
    """依次查找 coding_agent/.env 和项目根 .env。"""
    pkg_env = Path(__file__).parent / ".env"
    if pkg_env.exists():
        return str(pkg_env)
    root_env = Path(__file__).parent.parent / ".env"
    if root_env.exists():
        return str(root_env)
    return str(pkg_env)


class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_find_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── API Keys ──
    gemini_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # ── LLM ──
    llm_provider: str = Field(default="gemini", alias="AGENT_PROVIDER")
    llm_model: str = Field(default="gemini-2.5-flash", alias="AGENT_MODEL")
    known_models: list[str] = [
        "gemini-2.5-flash",
        "gemini-2.5-pro-preview-05-06",
        "gpt-4o",
        "gpt-4o-mini",
        "claude-sonnet-4-20250514",
        "claude-haiku-4-20250414",
    ]

    # ── 路径 ──
    workdir: Path = Field(default_factory=lambda: Path.cwd())
    output_dir: Path | None = None  # 默认 workdir/.agent
    pkg_dir: Path = Path(__file__).parent

    # ── Agent 行为 ──
    max_tool_rounds: int = 40
    context_limit: int = 800_000
    subagent_max_steps: int = 20
    subagent_result_max_chars: int = 8000
    plan_reminder_interval: int = 5

    # ── 工具限制 ──
    list_dir_max_depth: int = 6
    list_dir_max_entries: int = 200
    skip_dir_names: set[str] = {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        ".tox", "dist", "build", ".mypy_cache", ".ruff_cache",
    }
    grep_max_matches: int = 200
    glob_max_results: int = 300
    bash_timeout: int = 120
    python_timeout: int = 120
    http_max_bytes: int = 500_000
    http_timeout: int = 30

    # ── 上下文 / 输出 ──
    preview_chars: int = 300
    persist_threshold: int = 4000
    persist_preview_chars: int = 800
    keep_recent_tool_results: int = 6

    # ── 日志 / UI ──
    log_enabled: bool = True
    wait_indicator: bool = True
    todo_visual: bool = True
    need_notify: bool = Field(default=False, alias="AGENT_NOTIFY")

    # ── Hook 系统 ──
    hook_timeout: int = Field(default=30, alias="AGENT_HOOK_TIMEOUT")
    hook_sdk_mode: bool = Field(default=False, alias="AGENT_HOOK_SDK_MODE")

    # ── MCP / 插件（.claude-plugin/plugin.json）──
    mcp_enabled: bool = Field(default=True, alias="AGENT_MCP_ENABLED")
    mcp_permission_mode: str = Field(default="default", alias="AGENT_MCP_PERMISSION_MODE")

    # ── Ollama ──
    ollama_base_url: str = "http://localhost:11434"

    @property
    def resolved_output_dir(self) -> Path:
        return self.output_dir or (self.pkg_dir / ".agent")

    @property
    def log_dir(self) -> Path:
        return self.resolved_output_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "agent.log"

    @property
    def memory_file(self) -> Path:
        return self.resolved_output_dir / "memory.md"

    @property
    def memory_dir(self) -> Path:
        return self.resolved_output_dir / "memory"

    @property
    def tool_results_dir(self) -> Path:
        return self.resolved_output_dir / "tool_results"

    @property
    def transcript_dir(self) -> Path:
        return self.resolved_output_dir / "transcripts"

    @property
    def todo_file(self) -> Path:
        return self.resolved_output_dir / "todo.json"

    @property
    def todo_archive_dir(self) -> Path:
        return self.resolved_output_dir / "todo_archive"

    @property
    def tasks_dir(self) -> Path:
        return self.resolved_output_dir / "tasks"


# ── 全局单例 ──
CFG = AgentConfig()


# ── Prompt 常量 ──
# 注意：核心指令（CORE_INSTRUCTIONS）和记忆指南（MEMORY_GUIDANCE）
# 已迁移到 prompt.py，此处仅保留子代理的简短提示。

SUBAGENT_PROMPT = """你是一个专注执行单一子任务的 Coding Agent。
完成任务后用简洁文字返回结果，不要展开闲聊。
"""
