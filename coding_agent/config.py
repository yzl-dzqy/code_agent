# -*- coding: utf-8 -*-
"""全局配置：Pydantic Settings，从强类型 JSON / 环境变量自动加载。"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_json_config_file() -> Path | None:
    """查找可选 JSON 配置文件。"""
    explicit = os.environ.get("AGENT_CONFIG_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    pkg_dir = Path(__file__).parent
    candidates = (
        Path.cwd() / "agent_config.json",
        pkg_dir.parent / "agent_config.json",
        pkg_dir / "agent_config.json",
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def _load_json_config_data() -> dict[str, Any]:
    """读取并扁平化可选 JSON 配置，返回 AgentConfig 字段名。"""
    path = _find_json_config_file()
    if not path:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"JSON 配置文件不存在: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON 配置文件必须是对象: {path}")
    return _flatten_json_config(data)


def _flatten_json_config(data: dict[str, Any]) -> dict[str, Any]:
    """支持结构化 JSON 和环境变量式平铺 JSON。"""
    result: dict[str, Any] = {}

    env_key_map = {
        "GEMINI_API_KEY": "gemini_api_key",
        "OPENAI_API_KEY": "openai_api_key",
        "ANTHROPIC_API_KEY": "anthropic_api_key",
        "OPENAI_BASE_URL": "openai_base_url",
        "AGENT_PROVIDER": "llm_provider",
        "AGENT_MODEL": "llm_model",
        "KNOWN_MODELS": "known_models",
        "OLLAMA_BASE_URL": "ollama_base_url",
    }
    for key, value in data.items():
        if key in env_key_map:
            result[env_key_map[key]] = value
        elif key.islower():
            result[key] = value

    llm = data.get("llm")
    if isinstance(llm, dict):
        if llm.get("provider") is not None:
            result["llm_provider"] = llm["provider"]
        if llm.get("model") is not None:
            result["llm_model"] = llm["model"]
        if llm.get("known_models") is not None:
            result["known_models"] = llm["known_models"]

    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = data

    gemini = providers.get("gemini") if isinstance(providers, dict) else None
    if isinstance(gemini, dict):
        if gemini.get("api_key") is not None:
            result["gemini_api_key"] = gemini["api_key"]

    openai = providers.get("openai") if isinstance(providers, dict) else None
    if isinstance(openai, dict):
        if openai.get("api_key") is not None:
            result["openai_api_key"] = openai["api_key"]
        if openai.get("base_url") is not None:
            result["openai_base_url"] = openai["base_url"]

    anthropic = providers.get("anthropic") if isinstance(providers, dict) else None
    if isinstance(anthropic, dict):
        if anthropic.get("api_key") is not None:
            result["anthropic_api_key"] = anthropic["api_key"]

    claude = providers.get("claude") if isinstance(providers, dict) else None
    if isinstance(claude, dict):
        if claude.get("api_key") is not None:
            result["anthropic_api_key"] = claude["api_key"]

    ollama = providers.get("ollama") if isinstance(providers, dict) else None
    if isinstance(ollama, dict):
        if ollama.get("base_url") is not None:
            result["ollama_base_url"] = ollama["base_url"]
    return result


class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return env_settings, init_settings, file_secret_settings

    @classmethod
    def load(cls, json_path: str | Path | None = None) -> "AgentConfig":
        """Load config from JSON defaults, with real environment variables overriding."""
        old = os.environ.get("AGENT_CONFIG_FILE")
        if json_path is not None:
            os.environ["AGENT_CONFIG_FILE"] = str(json_path)
        try:
            return cls(**_load_json_config_data())
        finally:
            if json_path is not None:
                if old is None:
                    os.environ.pop("AGENT_CONFIG_FILE", None)
                else:
                    os.environ["AGENT_CONFIG_FILE"] = old

    # ── API Keys ──
    gemini_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openai_base_url: str = ""

    # ── LLM ──
    llm_provider: str = Field(default="gemini", alias="AGENT_PROVIDER")
    llm_model: str = Field(default="gemini-2.5-flash", alias="AGENT_MODEL")
    known_models: list[str] = [
        "gemini:gemini-2.5-flash",
        "gemini:gemini-2.5-pro-preview-05-06",
        "openai:gpt-4o",
        "openai:gpt-4o-mini",
        "claude:claude-sonnet-4-20250514",
        "claude:claude-haiku-4-20250414",
        "ollama:qwen2.5-coder:7b",
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
    trace_enabled: bool = True
    wait_indicator: bool = True
    todo_visual: bool = True
    need_notify: bool = Field(default=False, alias="AGENT_NOTIFY")

    # ── Session checkpoint ──
    session_checkpoint_enabled: bool = Field(default=True, alias="AGENT_SESSION_CHECKPOINT")
    session_resume_on_start: bool = Field(default=False, alias="AGENT_SESSION_RESUME")

    # ── Hook 系统 ──
    hook_timeout: int = Field(default=30, alias="AGENT_HOOK_TIMEOUT")
    hook_sdk_mode: bool = Field(default=False, alias="AGENT_HOOK_SDK_MODE")

    # ── MCP / 插件（.claude-plugin/plugin.json）──
    mcp_enabled: bool = Field(default=True, alias="AGENT_MCP_ENABLED")
    mcp_permission_mode: str = Field(default="default", alias="AGENT_MCP_PERMISSION_MODE")
    tool_permission_mode: str = Field(default="auto", alias="AGENT_TOOL_PERMISSION_MODE")

    # ── Ollama ──
    ollama_base_url: str = "http://localhost:11434"

    @property
    def resolved_output_dir(self) -> Path:
        return self.output_dir or (self.workdir / ".agent")

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
    def trace_file(self) -> Path:
        return self.resolved_output_dir / "traces" / "agent_trace.jsonl"

    @property
    def session_checkpoint_file(self) -> Path:
        return self.resolved_output_dir / "session_checkpoint.json"

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
CFG = AgentConfig.load()


# ── Prompt 常量 ──
# 注意：核心指令（CORE_INSTRUCTIONS）和记忆指南（MEMORY_GUIDANCE）
# 已迁移到 prompt.py，此处仅保留子代理的简短提示。

SUBAGENT_PROMPT = """你是一个专注执行单一子任务的 Coding Agent。
完成任务后用简洁文字返回结果，不要展开闲聊。
"""
