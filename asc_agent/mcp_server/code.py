"""代码侧检索：在头文件/实现/样例里定位符号与示例工程。

资料库索引只覆盖 Markdown，但「这个 API 的真实签名是什么」「有没有可
能直接跑的样例」必须看源码。这里做的是受控的正则扫描，不做全文索引。
"""

from __future__ import annotations

import re
from pathlib import Path

from asc_agent import config

SCOPES = {
    "include": "include",
    "impl": "impl",
    "examples": "examples",
}

SKIP_DIRS = {".git", "node_modules", "__pycache__", "build", "output", ".vscode"}
CODE_EXT = {".h", ".hpp", ".cpp", ".cc", ".c", ".asc", ".py", ".txt", ".cmake"}
MAX_FILE_BYTES = 2_000_000


def _iter_files(root: Path, exts: set[str], limit: int = 20000):
    count = 0
    for path in root.rglob("*"):
        if count >= limit:
            return
        if not path.is_file():
            continue
        if path.suffix.lower() not in exts:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path
        count += 1


def _definition_re(name: str) -> re.Pattern[str]:
    """匹配「函数/类定义」形态，避免把调用点当成定义。"""
    n = re.escape(name)
    return re.compile(
        rf"(?:__aicore__|__global__|__inline__|inline|static|template[^\n]*)?[^\n;{{}}]*\b{n}\s*\(",
    )


def find_symbol(name: str, scope: str = "all", limit: int = 10) -> dict:
    """在源码里找符号定义/声明。"""
    name = (name or "").strip()
    if not name:
        return {"name": name, "found": False, "error": "符号名为空"}
    roots: list[tuple[str, Path]] = []
    if scope in ("all", "include"):
        roots.append(("include", config.DEVKIT_ROOT / SCOPES["include"]))
    if scope in ("all", "impl"):
        roots.append(("impl", config.DEVKIT_ROOT / SCOPES["impl"]))
    if scope in ("all", "examples"):
        roots.append(("examples", config.DEVKIT_ROOT / SCOPES["examples"]))
    roots = [(tag, p) for tag, p in roots if p.exists()]
    if not roots:
        return {"name": name, "found": False, "error": "源码目录不存在"}

    pattern = _definition_re(name)
    bare = re.compile(rf"\b{re.escape(name)}\b")
    hits: list[dict] = []
    # 每个根目录各留一份配额：早期实现用一个全局上限，include 一扫满就直接
    # break，`scope="all"` 实际上永远看不到 impl 和 examples 的命中。
    per_root_cap = max(limit, 8)
    for tag, root in roots:
        root_hits = 0
        for path in _iter_files(root, CODE_EXT):
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if name not in text:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if not bare.search(line):
                    continue
                is_def = bool(pattern.search(line)) and not line.strip().startswith("//")
                hits.append(
                    {
                        "scope": tag,
                        "path": str(path.relative_to(config.DEVKIT_ROOT)).replace("\\", "/"),
                        "line": i,
                        "text": line.strip()[:240],
                        "definition": is_def,
                    }
                )
                root_hits += 1
                if root_hits >= per_root_cap:
                    break
            if root_hits >= per_root_cap:
                break
    hits.sort(key=lambda h: (not h["definition"], h["scope"] != "include", h["path"]))
    scopes_hit = sorted({h["scope"] for h in hits})
    return {
        "name": name,
        "scope": scope,
        "found": bool(hits),
        "count": len(hits),
        "hits": hits[:limit],
        "scopes_hit": scopes_hit,
        "note": "" if hits else "未找到，可换用 find_examples 或 search_docs。",
    }


def find_examples(keyword: str, limit: int = 8) -> dict:
    """按关键字找样例工程目录（以样例根目录聚合，方便直接抄工程结构）。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return {"keyword": keyword, "found": False, "error": "关键字为空"}
    root = config.DEVKIT_ROOT / "examples"
    if not root.exists():
        return {"keyword": keyword, "found": False, "error": "样例目录不存在"}
    tokens = [t for t in re.split(r"[\s,，]+", kw) if t]
    scored: dict[str, dict] = {}
    for path in _iter_files(root, {".asc", ".md", ".cpp", ".h", ".py"}):
        rel = str(path.relative_to(root)).replace("\\", "/")
        low = rel.lower()
        if not all(t in low for t in tokens):
            continue
        # 用两级目录做「工程根」，避免一个样例刷屏
        parts = rel.split("/")
        proj = "/".join(parts[:3]) if len(parts) >= 3 else "/".join(parts[:-1])
        entry = scored.setdefault(proj, {"project": proj, "files": 0, "samples": []})
        entry["files"] += 1
        if len(entry["samples"]) < 3:
            entry["samples"].append(rel)
    items = sorted(scored.values(), key=lambda e: (-e["files"], e["project"]))[:limit]
    return {
        "keyword": keyword,
        "found": bool(items),
        "count": len(items),
        "projects": items,
        "hint": "拿到 project 后可以用 read_doc 读它的 README.md，或用读源码的能力看 .asc 主文件。",
    }
