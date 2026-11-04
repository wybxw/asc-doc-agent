"""Markdown 解析：抽取标题、章节、芯片支持标签、头文件路径、代码块。

只做「结构化降噪」，不追求完整的 CommonMark 兼容——目标是把文档切成
既能整段喂给 LLM、又能被 FTS 命中的语义块。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# <!-- npu="950" id1 --> ... <!-- end id1 -->
NPU_TAG = re.compile(r'<!--\s*npu="([^"]+)"\s*(id\d+)?\s*-->')

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)

FENCE = re.compile(r"^```([A-Za-z0-9_+-]*)\s*$", re.M)

# 支持矩阵里常见写法：`- Ascend 950PR/Ascend 950DT：支持`
SUPPORT_LINE = re.compile(r"^-\s*(.+?)[：:]\s*(支持|不支持)", re.M)

HEADER_PATH = re.compile(
    r"头文件路径为[：:]\s*`?([^`\n。]+?)`?[。\n]", re.M
)
HEADER_PATH2 = re.compile(r"`([A-Za-z0-9_/]+\.h)`")

PROTO = re.compile(r"^__aicore__.*$", re.M)

# 芯片别名归一（npu 标签值 → 内部规范名）
CHIP_ALIASES = {
    "950": "ascend950",
    "a3": "atlas_a3",
    "910b": "ascend910b",
    "910": "ascend910",
    "310p": "ascend310p",
    "310b": "ascend310b",
    "x90": "ascend_x90",
    "9030": "ascend9030",
}

# 用户口语 → 规范芯片名（给 agent 的 chip 参数兜底）
CHIP_LOOKUP = {
    "950": "ascend950", "950pr": "ascend950", "950dt": "ascend950",
    "a3": "atlas_a3", "atlasa3": "atlas_a3", "a2": "ascend910b",
    "910b": "ascend910b", "910": "ascend910", "910a": "ascend910",
    "310p": "ascend310p", "310b": "ascend310b", "x90": "ascend_x90",
    "9030": "ascend9030", "610": "ascend310p",
}


@dataclass
class Section:
    heading: str  # 完整标题路径，如 "Exp > 参数说明"
    level: int
    body: str


@dataclass
class ParsedDoc:
    path: str
    title: str
    sections: list[Section] = field(default_factory=list)
    chips: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    prototypes: list[str] = field(default_factory=list)
    code_blocks: list[str] = field(default_factory=list)
    support: list[dict] = field(default_factory=list)


def _strip_anchor(title: str) -> str:
    """去掉 `# 标题<a name="...">` / `<a id="...">` 这类 HTML 锚点。"""
    t = re.sub(r"<a\s+[^>]*>.*?</a>", "", title, flags=re.S)
    t = re.sub(r"<[^>]+>", "", t)
    return t.strip()


def expand_chip_value(raw: str) -> list[str]:
    """把一个 npu 标签值展开成规范芯片名列表。

    标签允许写成合并形式（如 `npu="950,a3,910b"`、`npu="310p/310b"`），
    所以先按逗号/斜杠/空白拆分，再逐个映射；未知值原样保留以便发现缺口。
    """
    out: list[str] = []
    for part in re.split(r"[,，/;；\s]+", raw.strip().lower()):
        if not part:
            continue
        key = CHIP_ALIASES.get(part, part)
        if key not in out:
            out.append(key)
    return out


def extract_chips(text: str) -> list[str]:
    """从 npu 标签里抽取芯片集合（去重保序）。"""
    out: list[str] = []
    for m in NPU_TAG.finditer(text):
        for key in expand_chip_value(m.group(1)):
            if key not in out:
                out.append(key)
    # 没有标签时退化为扫描正文里的支持矩阵
    if not out:
        for alias, norm in (
            ("Ascend 950", "ascend950"),
            ("Ascend950", "ascend950"),
            ("Atlas A3", "atlas_a3"),
            ("Atlas A2", "ascend910b"),
            ("Ascend 910", "ascend910b"),
            ("Ascend 310P", "ascend310p"),
            ("Ascend 310B", "ascend310b"),
        ):
            if alias in text and norm not in out:
                out.append(norm)
    return out


NPU_BLOCK = re.compile(
    r'<!--\s*npu="([^"]+)"(?:\s+id\d+)?\s*-->\s*\n?(.*?)<!--\s*end[^>]*-->',
    re.S,
)


def extract_support_matrix(text: str) -> list[dict]:
    """解析「产品支持情况」里的 npu 标签块，保留 芯片→原型→支持/不支持 的粒度。

    `### 不传入config的原型` 这类三级标题会被记录成 `prototype` 字段。
    返回形如
    `[{"chip":"ascend950","prototype":"不传入config的原型","product":"Ascend 950PR/Ascend 950DT","supported":false}]`
    """
    out: list[dict] = []
    prototype = ""
    pos = 0
    events: list[tuple[int, str, re.Match]] = []
    for m in NPU_BLOCK.finditer(text):
        events.append((m.start(), "block", m))
    for m in re.finditer(r"^###\s+(.+?)\s*$", text, re.M):
        events.append((m.start(), "proto", m))
    events.sort(key=lambda e: e[0])

    for _, kind, m in events:
        if kind == "proto":
            prototype = _strip_anchor(m.group(1))
            continue
        chips = expand_chip_value(m.group(1)) or [m.group(1).strip().lower()]
        for line in m.group(2).splitlines():
            s = line.strip()
            mm = re.match(r"-\s*(.+?)[：:]\s*(支持|不支持)", s)
            if not mm:
                continue
            for chip in chips:
                out.append(
                    {
                        "chip": chip,
                        "prototype": prototype,
                        "product": mm.group(1).strip(),
                        "supported": mm.group(2) == "支持",
                    }
                )
    return out


def extract_headers(text: str) -> list[str]:
    found = [m.group(1).strip() for m in HEADER_PATH.finditer(text)]
    if not found:
        found = [m.group(1) for m in HEADER_PATH2.finditer(text[:4000])]
    seen: list[str] = []
    for h in found:
        h = h.strip().strip('`"\' ')
        if h and h not in seen:
            seen.append(h)
    return seen[:6]


def split_sections(text: str, doc_title: str) -> list[Section]:
    """按 `##` 级标题切块，保留标题路径做上下文。"""
    marks = list(HEADING.finditer(text))
    if not marks:
        return [Section(heading=doc_title, level=1, body=text.strip())]

    sections: list[Section] = []
    h2_stack: list[str] = []
    for idx, m in enumerate(marks):
        level = len(m.group(1))
        title = _strip_anchor(m.group(2))
        start = m.end()
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
        body = text[start:end].strip()
        if level == 1:
            continue  # 文档大标题单独存，不占 chunk
        if level == 2:
            h2_stack = [title]
        elif len(h2_stack) == 1:
            h2_stack = [h2_stack[0], title]
        else:
            h2_stack = h2_stack[:2] + [title]
        if not body:
            continue
        sections.append(
            Section(heading=" > ".join(h2_stack), level=level, body=body)
        )
    if not sections:
        sections = [Section(heading=doc_title, level=1, body=text.strip())]
    return sections


def chunk_section(sec: Section, max_chars: int = 1400) -> list[str]:
    """超长章节按空行段落再切，带 120 字重叠避免截断语义。"""
    if len(sec.body) <= max_chars:
        return [sec.body]
    paras = re.split(r"\n\s*\n", sec.body)
    out: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = f"{buf}\n\n{p}".strip()
        else:
            if buf:
                out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out


def parse_markdown(path: Path) -> ParsedDoc:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = HEADING.search(text)
    title = _strip_anchor(m.group(2)) if m else path.stem

    blocks = re.findall(r"^```[A-Za-z0-9_+-]*\s*\n(.*?)^```", text, re.S | re.M)
    parsed = ParsedDoc(
        path=str(path).replace("\\", "/"),
        title=title,
        sections=split_sections(text, title),
        chips=extract_chips(text),
        headers=extract_headers(text),
        prototypes=[p.strip() for p in PROTO.findall(text)][:8],
        code_blocks=blocks[:5],
        support=extract_support_matrix(text),
    )
    return parsed


def classify_path(rel: str) -> tuple[str, str]:
    """(kind, category) —— 依据仓库相对路径推断。"""
    rel = rel.replace("\\", "/")
    if rel.startswith(".agent/skills/"):
        parts = rel.split("/")
        skill = parts[2] if len(parts) > 2 else ""
        return "skill", skill
    if "/examples/" in rel or rel.startswith("examples/"):
        parts = rel.split("/")
        return "example", "/".join(parts[:3])
    if "/api/" in rel:
        # docs/zh/api/SIMD-API/basic_api/math_compute/Exp.md
        seg = rel.split("/api/", 1)[1]
        parts = seg.split("/")
        return "api_doc", "/".join(parts[:-1])
    if "/guide/" in rel:
        seg = rel.split("/guide/", 1)[1]
        parts = seg.split("/")
        return "guide", "/".join(parts[:-1]) if len(parts) > 1 else "guide"
    return "doc", "/".join(rel.split("/")[:-1])


def api_name_from(rel: str, title: str) -> str:
    """API 名通常等于文件名去扩展名，如 Exp.md -> Exp。"""
    stem = Path(rel).stem
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", stem):
        return stem
    return title.split("(")[0].strip() or stem
