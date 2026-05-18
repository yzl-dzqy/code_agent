# -*- coding: utf-8 -*-
"""网络工具：HTTP GET。"""

from __future__ import annotations

import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ...config import CFG
from ...utils.log import log, preview_text
from ..registry import tool


def _host_allowed(host: str) -> bool:
    allow = os.getenv("AGENT_HTTP_ALLOW", "*").strip()
    if allow == "*":
        return True
    host = (host or "").lower()
    return any(
        host == e.strip().lower() or host.endswith("." + e.strip().lower())
        for e in allow.split(",") if e.strip()
    )


@tool(name="http_get", description="HTTP GET 获取 URL 文本正文")
def http_get(url: str, max_bytes: int = 0) -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "Error: 仅支持 http/https"
        if not parsed.hostname:
            return "Error: 无效 URL"
        if not _host_allowed(parsed.hostname):
            return "Error: 主机不在白名单"
        limit = min(max_bytes or CFG.http_max_bytes, CFG.http_max_bytes)
        req = Request(url, headers={"User-Agent": "coding-agent/2.0"})
        with urlopen(req, timeout=CFG.http_timeout) as resp:
            raw = resp.read(limit + 1)
        truncated = len(raw) > limit
        text = raw[:limit].decode("utf-8", errors="replace")
        if truncated:
            text += f"\n...[截断，上限 {limit} 字节]"
        log("INFO", "http_get", f"url={preview_text(url, 80)}, bytes={len(raw)}")
        return text
    except HTTPError as exc:
        return f"Error: HTTP {exc.code} {exc.reason}"
    except URLError as exc:
        return f"Error: {exc.reason}"
    except Exception as exc:
        return f"Error: {exc}"
