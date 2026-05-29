# -*- coding: utf-8 -*-
"""LLM Provider 注册表：根据名称/模型自动选择 provider。"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import AgentConfig
from .base import LLMProvider


PROVIDER_ALIASES = {
    "gemini": "gemini",
    "google": "gemini",
    "openai": "openai",
    "gpt": "openai",
    "claude": "claude",
    "anthropic": "claude",
    "ollama": "ollama",
    "local": "ollama",
}


@dataclass(frozen=True)
class ModelSelection:
    """一次模型选择的规范化结果。"""

    provider: str
    model: str

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.model}"


def normalize_provider(name: str) -> str:
    """规范化 provider 名称和别名。"""
    provider = PROVIDER_ALIASES.get((name or "").strip().lower())
    if not provider:
        raise ValueError("未知 provider: " + str(name) + "，可用: gemini, openai, claude, ollama")
    return provider


def infer_provider_from_model(model: str, default_provider: str) -> str:
    """根据常见模型名前缀推断 provider，无法推断时沿用当前 provider。"""
    lower = (model or "").strip().lower()
    if lower.startswith("gemini-"):
        return "gemini"
    if lower.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4")):
        return "openai"
    if lower.startswith("claude-"):
        return "claude"
    return normalize_provider(default_provider)


def parse_model_ref(ref: str, default_provider: str) -> ModelSelection:
    """
    解析模型引用。

    支持：
      - provider:model，例如 openai:gpt-4o
      - provider/model，例如 claude/claude-sonnet-4-20250514
      - 仅模型名，例如 gemini-2.5-flash / gpt-4o / 自定义本地模型
    """
    value = (ref or "").strip()
    if not value:
        raise ValueError("模型名称不能为空")

    for sep in (":", "/"):
        if sep not in value:
            continue
        prefix, model = value.split(sep, 1)
        if prefix.strip().lower() in PROVIDER_ALIASES:
            model = model.strip()
            if not model:
                raise ValueError("模型名称不能为空")
            return ModelSelection(normalize_provider(prefix), model)

    provider = infer_provider_from_model(value, default_provider)
    return ModelSelection(provider, value)


def get_provider(
    cfg: AgentConfig,
    *,
    provider_name: str | None = None,
    model_name: str | None = None,
) -> LLMProvider:
    """根据配置创建对应的 LLM provider 实例。"""
    name = normalize_provider(provider_name or cfg.llm_provider)
    model = model_name or cfg.llm_model

    if name == "gemini":
        from .gemini import GeminiProvider
        if not cfg.gemini_api_key:
            raise RuntimeError("未设置 GEMINI_API_KEY")
        return GeminiProvider(api_key=cfg.gemini_api_key, model=model)

    if name in ("openai", "gpt"):
        from .openai_provider import OpenAIProvider
        if not cfg.openai_api_key:
            raise RuntimeError("未设置 OPENAI_API_KEY")
        base_url = cfg.openai_base_url or None
        return OpenAIProvider(api_key=cfg.openai_api_key, model=model, base_url=base_url)

    if name in ("claude", "anthropic"):
        from .claude import ClaudeProvider
        if not cfg.anthropic_api_key:
            raise RuntimeError("未设置 ANTHROPIC_API_KEY")
        return ClaudeProvider(api_key=cfg.anthropic_api_key, model=model)

    if name in ("ollama", "local"):
        from .local import OllamaProvider
        return OllamaProvider(model=model, base_url=cfg.ollama_base_url.rstrip("/") + "/v1")

    raise ValueError(f"未知 provider: {name}，可用: gemini, openai, claude, ollama")
