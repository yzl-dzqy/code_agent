# -*- coding: utf-8 -*-
"""全局配置：Pydantic Settings，从 JSON / 环境变量自动加载。"""

from __future__ import annotations

import json
import os
from pathlib import Path

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


def _json_value_to_env(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _set_env_default(name: str, value: object) -> None:
    if value is None:
        return
    os.environ.setdefault(name, _json_value_to_env(value))


def _load_json_config_into_env() -> None:
    """
    将可选 JSON 配置写入环境变量默认值。

    优先级：命令行/真实环境变量 > JSON > 代码默认值。
    """
    path = _find_json_config_file()
    if not path:
        return
    if not path.exists():
        raise FileNotFoundError(f"JSON 配置文件不存在: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON 配置文件必须是对象: {path}")

    # 支持平铺环境变量写法，例如 {"AGENT_MODEL": "gpt-4o"}。
    for key, value in data.items():
        if key.isupper():
            _set_env_default(key, value)

    llm = data.get("llm")
    if isinstance(llm, dict):
        _set_env_default("AGENT_PROVIDER", llm.get("provider"))
        _set_env_default("AGENT_MODEL", llm.get("model"))
        _set_env_default("KNOWN_MODELS", llm.get("known_models"))

    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = data

    gemini = providers.get("gemini") if isinstance(providers, dict) else None
    if isinstance(gemini, dict):
        _set_env_default("GEMINI_API_KEY", gemini.get("api_key"))

    openai = providers.get("openai") if isinstance(providers, dict) else None
    if isinstance(openai, dict):
        _set_env_default("OPENAI_API_KEY", openai.get("api_key"))
        _set_env_default("OPENAI_BASE_URL", openai.get("base_url"))

    anthropic = providers.get("anthropic") if isinstance(providers, dict) else None
    if isinstance(anthropic, dict):
        _set_env_default("ANTHROPIC_API_KEY", anthropic.get("api_key"))

    claude = providers.get("claude") if isinstance(providers, dict) else None
    if isinstance(claude, dict):
        _set_env_default("ANTHROPIC_API_KEY", claude.get("api_key"))

    ollama = providers.get("ollama") if isinstance(providers, dict) else None
    if isinstance(ollama, dict):
        _set_env_default("OLLAMA_BASE_URL", ollama.get("base_url"))


_load_json_config_into_env()


class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
    )

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
