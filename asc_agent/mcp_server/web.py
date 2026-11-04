"""外部资料抓取：把在线文档/网页转成纯文本。

为什么需要它：资料库是快照，而「上游文档更新了没」「这个链接里怎么写」
这类问题必须现抓。抓回来要洗 HTML，因为 gitcode 的 raw 链接返回的是
带 `<div>` 外壳的页面（已实测），直接喂原文给模型等于喂噪音。
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urlparse

import httpx

ALLOWED_SCHEMES = {"http", "https"}
DEFAULT_MAX_CHARS = 6000
TIMEOUT = 20.0
UA = "asc-doc-agent/0.1 (+local study project)"

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.S | re.I)
_BLOCK_RE = re.compile(r"</?(p|div|br|li|tr|h[1-6]|pre|section|article)\b[^>]*>", re.I)
_BLANKS = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    """粗粒度 HTML → 纯文本（保留段落换行，丢掉脚本样式）。"""
    text = _SCRIPT_RE.sub(" ", html)
    # 代码块先抽出来，避免 `<` `>` 被当成标签吃掉
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip()


def _looks_like_html(text: str) -> bool:
    head = text[:400].lower()
    return "<!doctype" in head or "<html" in head or "<body" in head


def fetch_url(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """抓一个 http(s) 页面并转文本。失败时不抛异常，返回带 error 的字典。"""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        return {"url": url, "found": False, "error": f"只支持 http/https，收到 {parsed.scheme!r}"}
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": UA}) as c:
            resp = c.get(url)
    except httpx.HTTPError as exc:
        return {"url": url, "found": False, "error": f"请求失败：{type(exc).__name__}: {exc}"}
    if resp.status_code >= 400:
        return {"url": url, "found": False, "error": f"HTTP {resp.status_code}", "status": resp.status_code}
    raw = resp.text
    is_html = _looks_like_html(raw) or "text/html" in resp.headers.get("content-type", "")
    text = html_to_text(raw) if is_html else raw
    return {
        "url": str(resp.url),
        "found": True,
        "status": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "cleaned_html": is_html,
        "chars": len(text),
        "truncated": len(text) > max_chars,
        "text": text[:max_chars],
    }
