"""本地资料库的直读层：按路径取原文、按章节裁剪、列目录。

和检索层分开是因为语义不同：检索是「我不知道在哪」，直读是
「我已经知道在哪，把原文给我」。agent 的典型链路是 search → read，
所以两层都要有，且 read 必须能只取一段（否则一篇 API 文档几千字会撑爆上下文）。
"""

from __future__ import annotations

import re
from pathlib import Path

from asc_agent import config

DEFAULT_MAX_CHARS = 4000


class PathOutsideRepo(ValueError):
    """请求的路径跑出了资料库根目录。"""


def resolve(rel_path: str) -> Path:
    """把路径解析成绝对路径。

    相对路径按资料库根解析并挡住目录穿越（防止 `../../etc/passwd`）；
    **绝对路径原样使用**——用户要检查自己写的文档时给的就是绝对路径，
    限制成只能在资料库内会让这个能力没法用。
    """
    raw = (rel_path or "").strip()
    if not raw:
        raise PathOutsideRepo("路径为空")
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    root = config.DEVKIT_ROOT.resolve()
    target = (root / raw.replace("\\", "/").lstrip("/")).resolve()
    if root != target and root not in target.parents:
        raise PathOutsideRepo(f"路径越界：{rel_path}")
    return target


def split_sections(text: str) -> list[tuple[str, int, str]]:
    """按 `##` 切章节，返回 (标题, 起始行号, 正文)。"""
    out: list[tuple[str, int, str]] = []
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.M))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        line = text.count("\n", 0, m.start()) + 1
        out.append((m.group(1).strip(), line, text[start:end].strip()))
    return out


def read_doc(rel_path: str, section: str | None = None, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """读一篇文档（可只取命中章节）。"""
    try:
        path = resolve(rel_path)
    except PathOutsideRepo as exc:
        return {"path": rel_path, "found": False, "error": str(exc)}
    if not path.exists():
        return {"path": rel_path, "found": False, "error": "文件不存在"}
    if path.suffix.lower() not in {".md", ".txt", ".h", ".hpp", ".cpp", ".asc", ".py", ".csv"}:
        return {"path": rel_path, "found": False, "error": f"不支持的文件类型：{path.suffix}"}
    text = path.read_text(encoding="utf-8", errors="replace")
    total = len(text)
    sections = split_sections(text)
    picked: list[dict] = []
    if section:
        for heading, line, body in sections:
            if section in heading:
                picked.append({"heading": heading, "line": line, "body": body[:max_chars]})
    if section and not picked:
        return {
            "path": rel_path,
            "found": False,
            "error": f"未找到包含「{section}」的章节",
            "sections": [s[0] for s in sections],
        }
    if picked:
        return {
            "path": rel_path,
            "found": True,
            "total_chars": total,
            "sections": picked,
            "truncated": any(len(p["body"]) >= max_chars for p in picked),
        }
    body = text[:max_chars]
    return {
        "path": rel_path,
        "found": True,
        "total_chars": total,
        "body": body,
        "truncated": total > max_chars,
        "sections": [s[0] for s in sections],
    }


def read_skill(skill: str, file: str = "SKILL.md") -> dict:
    """读 .agent/skills 下的技能文件（技能名做白名单校验，避免任意路径读取）。"""
    root = config.skills_root()
    if not root.exists():
        return {"skill": skill, "found": False, "error": "找不到技能目录"}
    skill = (skill or "").strip().strip("/")
    # 允许「带 namespace 的技能名」和嵌套目录，但不能逃出 skills 根
    target = (root / skill / file.replace("\\", "/").lstrip("/")).resolve()
    if root.resolve() not in target.parents:
        return {"skill": skill, "found": False, "error": "路径越界"}
    if not target.exists():
        available = sorted(
            p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
        ) if root.is_dir() else []
        return {
            "skill": skill,
            "file": file,
            "found": False,
            "error": "技能文件不存在",
            "available_skills": available,
        }
    text = target.read_text(encoding="utf-8", errors="replace")
    return {
        "skill": skill,
        "file": file,
        "found": True,
        "chars": len(text),
        "body": text[:12000],
    }
