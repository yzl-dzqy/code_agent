# -*- coding: utf-8 -*-
"""LLM Provider 注册表：根据名称/模型自动选择 provider。"""

from __future__ import annotations

from ..config import AgentConfig
from .base import LLMProvider


def get_provider(cfg: AgentConfig) -> LLMProvider:
    """根据配置创建对应的 LLM provider 实例。"""
    name = cfg.llm_provider.lower()

    if name == "gemini":
        from .gemini import GeminiProvider
        if not cfg.gemini_api_key:
            raise RuntimeError("未设置 GEMINI_API_KEY")
        return GeminiProvider(api_key=cfg.gemini_api_key, model=cfg.llm_model)

    if name in ("openai", "gpt"):
        from .openai_provider import OpenAIProvider
        if not cfg.openai_api_key:
            raise RuntimeError("未设置 OPENAI_API_KEY")
        return OpenAIProvider(api_key=cfg.openai_api_key, model=cfg.llm_model)

    if name in ("claude", "anthropic"):
        from .claude import ClaudeProvider
        if not cfg.anthropic_api_key:
            raise RuntimeError("未设置 ANTHROPIC_API_KEY")
        return ClaudeProvider(api_key=cfg.anthropic_api_key, model=cfg.llm_model)

    if name in ("ollama", "local"):
        from .local import OllamaProvider
        return OllamaProvider(model=cfg.llm_model, base_url=cfg.ollama_base_url + "/v1")

    raise ValueError(f"未知 provider: {name}，可用: gemini, openai, claude, ollama")
