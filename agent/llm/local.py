# -*- coding: utf-8 -*-
"""本地模型 provider（Ollama），复用 OpenAI 兼容 API。"""

from __future__ import annotations

from .openai_provider import OpenAIProvider


class OllamaProvider(OpenAIProvider):
    """通过 Ollama 的 OpenAI 兼容接口访问本地模型。"""

    def __init__(self, model: str = "qwen2.5-coder:7b", base_url: str = "http://localhost:11434/v1"):
        super().__init__(
            api_key="ollama",  # Ollama 不需要真实 key
            model=model,
            base_url=base_url,
        )

    def count_tokens(self, text: str) -> int:
        return len(text) // 4
