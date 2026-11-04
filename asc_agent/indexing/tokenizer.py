"""中文友好的分词器（bigram）。

SQLite FTS5 自带的 `unicode61` 分词器会把一整段中文当成一个 token，
导致「资料助手」搜不到「资料」；`trigram` 又要 3 字以上才好用。
这里采用业界常用的做法：**索引前**把中文切成二元组（bigram）并用空格隔开，
**查询前**用同样的规则切词，多字中文词再包成 FTS5 phrase（`"a b"`），
既保证召回又保证「相邻才算命中」的精度。
"""

from __future__ import annotations

import re

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_RUN = re.compile(f"[{_CJK}]+")
_TOKEN = re.compile(f"[{_CJK}]+|[A-Za-z_][A-Za-z0-9_]*|[0-9]+(?:\\.[0-9]+)?")


def _split_cjk(run: str) -> list[str]:
    """把一个连续中文串切成 bigram；单字保留单字。"""
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def tokenize(text: str) -> str:
    """把原文切成「用空格分隔的索引 token 串」。"""
    if not text:
        return ""
    out: list[str] = []
    for m in _TOKEN.finditer(text):
        piece = m.group(0)
        if _CJK_RUN.fullmatch(piece):
            out.extend(_split_cjk(piece))
        else:
            out.append(piece.lower())
    return " ".join(out)


def _escape(term: str) -> str:
    return term.replace('"', "")


def build_match_query(query: str, operator: str = "OR") -> str:
    """把自然语言查询转成 FTS5 MATCH 表达式。

    - 中文词 → phrase：`资料助手` → `"资料 料助"`
    - 英文/数字 → 前缀词：`Cast` → `cast*`
    - 多个词之间用 operator（OR 召回优先，AND 精度优先）
    """
    terms: list[str] = []
    for m in _TOKEN.finditer(query or ""):
        piece = m.group(0)
        if _CJK_RUN.fullmatch(piece):
            grams = _split_cjk(piece)
            if len(grams) == 1:
                terms.append(f'"{_escape(grams[0])}"')
            else:
                terms.append('"' + " ".join(_escape(g) for g in grams) + '"')
        else:
            low = piece.lower()
            if low:
                terms.append(f'"{_escape(low)}"*')
    if not terms:
        return ""
    joiner = " AND " if operator.upper() == "AND" else " OR "
    return joiner.join(terms)


def highlight_terms(query: str) -> list[str]:
    """返回用于 Python 侧复核/高亮的关键词原文。"""
    out: list[str] = []
    for m in _TOKEN.finditer(query or ""):
        piece = m.group(0)
        if _CJK_RUN.fullmatch(piece):
            if len(piece) > 2:
                out.extend(piece[i : i + 2] for i in range(len(piece) - 1))
            else:
                out.append(piece)
        else:
            out.append(piece.lower())
    return out
