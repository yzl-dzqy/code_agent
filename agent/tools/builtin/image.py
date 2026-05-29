# -*- coding: utf-8 -*-
"""图像分析工具：通过 LLM 视觉能力分析图片。"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from ...config import CFG
from ...utils.log import log
from ..registry import tool

# 运行时由 agent 注入 LLM provider
_llm_provider = None


def set_llm_provider(provider):
    global _llm_provider
    _llm_provider = provider


# @tool(name="read_image", description="读取并分析图片内容", parallel=False)
# def read_image(path: str, question: str = "详细描述这张图片的内容。") -> str:
#     try:
#         fp = (CFG.workdir / path).resolve()
#         if not fp.is_relative_to(CFG.workdir):
#             return "Error: 路径逃出工作区"
#         if not fp.exists():
#             return f"Error: 文件不存在: {path}"

#         # 仅 Gemini 支持原生视觉，其他 provider 可后续扩展
#         if _llm_provider is None:
#             return "Error: 未初始化 LLM provider"

#         image_bytes = fp.read_bytes()
#         mime = mimetypes.guess_type(path)[0] or "image/jpeg"
#         log("INFO", "read_image", f"path={path}, mime={mime}, bytes={len(image_bytes)}")

#         from google import genai
#         image_part = genai.types.Part.from_bytes(data=image_bytes, mime_type=mime)
#         # 直接用 Gemini client 的视觉能力
#         if hasattr(_llm_provider, "_client"):
#             resp = _llm_provider._client.models.generate_content(
#                 model=_llm_provider.model_name,
#                 contents=[image_part, question],
#             )
#             return (resp.text or "").strip() or "(无结果)"
#         return "Error: 当前 provider 不支持视觉分析"
#     except Exception as exc:
#         log("ERROR", "read_image_error", str(exc))
#         return f"Error: {exc}"
