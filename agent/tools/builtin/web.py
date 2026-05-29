# -*- coding: utf-8 -*-
"""增强网页工具：web_search / web_fetch。"""

from __future__ import annotations

import json
import re
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

from ...config import CFG
from ..registry import tool


def _host_allowed(host: str) -> bool:
    allow = (host or "").strip().lower()
    if not allow:
        return False
    deny_local = {"localhost", "127.0.0.1", "::1"}
    if allow in deny_local:
        return False
    return True


def _clean_text(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _html_to_markdown(html: str) -> str:
    # 中文注释：这是轻量 HTML->Markdown 转换，避免引入重依赖。
    text = re.sub(r"(?is)<script.*?>.*?</script>", "", html)
    text = re.sub(r"(?is)<style.*?>.*?</style>", "", text)
    text = re.sub(r"(?i)</h1>", "\n\n", text)
    text = re.sub(r"(?i)</h2>", "\n\n", text)
    text = re.sub(r"(?i)</h3>", "\n\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)<br\\s*/?>", "\n", text)
    text = re.sub(r"(?i)<li>", "- ", text)
    text = re.sub(r"(?is)<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"[\2](\1)", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    return _clean_text(unescape(text))


@tool(
    name="web_fetch",
    description="抓取网页并返回 Markdown 文本",
    when_to_use="当你已经有 URL，需要抓取页面正文并转成可读文本时使用。",
    search_hint="fetch webpage url markdown",
    should_defer=True,
    parallel=False,
)
def web_fetch(url: str, max_bytes: int = 0) -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "Error: 仅支持 http/https URL"
        if not parsed.hostname or not _host_allowed(parsed.hostname):
            return "Error: 不允许访问该主机"
        cap = min(max_bytes or CFG.http_max_bytes, CFG.http_max_bytes)
        req = Request(
            url,
            headers={"User-Agent": "coding-agent/2.0 (+web_fetch)"},
        )
        with urlopen(req, timeout=CFG.http_timeout) as resp:
            raw = resp.read(cap + 1)
        truncated = len(raw) > cap
        html = raw[:cap].decode("utf-8", errors="replace")
        md = _html_to_markdown(html)
        if truncated:
            md += f"\n\n...[截断，上限 {cap} 字节]"
        return md or "(空内容)"
    except HTTPError as exc:
        return f"Error: HTTP {exc.code} {exc.reason}"
    except URLError as exc:
        return f"Error: {exc.reason}"
    except Exception as exc:
        return f"Error: {exc}"


@tool(
    name="web_search",
    description="通过 DuckDuckGo HTML 检索网页并返回结构化结果",
    when_to_use="当你没有明确 URL，需要先搜集候选页面时使用。",
    search_hint="search web query results",
    should_defer=True,
    parallel=False,
)
def web_search(query: str, limit: int = 5) -> str:
    try:
        q = (query or "").strip()
        if not q:
            return "Error: query 不能为空"
        cap = max(1, min(limit, 10))
        url = f"https://duckduckgo.com/html/?q={quote_plus(q)}"
        req = Request(url, headers={"User-Agent": "coding-agent/2.0 (+web_search)"})
        with urlopen(req, timeout=CFG.http_timeout) as resp:
            html = resp.read(CFG.http_max_bytes).decode("utf-8", errors="replace")
        rows: list[dict[str, str]] = []
        pattern = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
            re.IGNORECASE,
        )
        for m in pattern.finditer(html):
            title = re.sub(r"(?is)<[^>]+>", "", m.group("title"))
            rows.append(
                {
                    "title": _clean_text(unescape(title)),
                    "url": unescape(m.group("href")),
                }
            )
            if len(rows) >= cap:
                break
        return json.dumps({"query": q, "count": len(rows), "results": rows}, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error: {exc}"
