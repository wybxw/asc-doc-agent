"""把 `asc-doc-checker` 技能的规范变成可执行检查。

技能本身是给人看的 308 行 Markdown 文档；agent 要「能查」就得把它仪器化。
这里挑出**可机器判定**的规则实现（标点空格、标题、代码块、图注、芯片标签、
章节顺序、术语、头文件写法、废弃接口），剩下的靠读 SKILL.md 人工判断。
"""

from __future__ import annotations

import re
from pathlib import Path

from asc_agent import config
from asc_agent.mcp_server.docs import resolve

# --- 规则 1：中文标点 & 中英文之间不加空格 -------------------------------
SPACE_CJK_EN = re.compile(
    r"[\u4e00-\u9fff][ \t]+[A-Za-z0-9]|[A-Za-z0-9][ \t]+[\u4e00-\u9fff]"
)
# 中文语境里出现英文标点：`调用,这里` / `说明:内容` / `结束.` 都该报
HALF_PUNCT_IN_CJK = re.compile(r"[\u4e00-\u9fff][,;:!?](?=[\u4e00-\u9fff\s]|$)")
# 句号单独处理：`中文.` 结尾可能是英文缩写（如 `e.g.`），只在后面接中文时判定
HALF_PERIOD_IN_CJK = re.compile(r"[\u4e00-\u9fff]\.(?=[\u4e00-\u9fff])")

# 官方产品名必须保留中间的空格（术语规范第 11 条：
# ✅ `Atlas A3 训练系列产品` / ❌ `Atlas A3训练系列产品`），所以不能一刀切报空格。
PRODUCT_NAMES = [
    "Atlas A3 训练系列产品/Atlas A3 推理系列产品",
    "Atlas A2 训练系列产品/Atlas A2 推理系列产品",
    "Atlas A2 训练系列产品",
    "Atlas A3 训练系列产品",
    "Atlas 推理系列产品",
    "Atlas 训练系列产品",
    "Atlas 200I/500 A2 推理产品",
    "Atlas 推理系列产品AI Core",
    "Atlas 推理系列产品Vector Core",
    "Ascend 950PR/Ascend 950DT",
    "Ascend 950PR",
    "Ascend 950DT",
    "Kirin X90",
    "Kirin 9030",
]


def _strip_product_names(line: str) -> str:
    """把官方产品名从行里挖掉，避免产品名自带空格被误报为「中英文之间加空格」。"""
    for name in sorted(PRODUCT_NAMES, key=len, reverse=True):
        line = line.replace(name, "")
    return line


INLINE_CODE = re.compile(r"`[^`]*`")
LINK_TEXT = re.compile(r"\[[^\]]*\]\([^)]*\)")


def _strip_inline_code(line: str) -> str:
    """去掉行内代码，避免把 `Location::L0C` 这类枚举值当成术语误报。"""
    return INLINE_CODE.sub("", line)


def _strip_link_text(line: str) -> str:
    """去掉链接文字，避免把 `[Int8 Channel Merge](...)` 这种特性名当成 dtype 误报。"""
    return LINK_TEXT.sub("", line)

# --- 规则 2：标题 ---------------------------------------------------------
HEADING_BAD_SPACE = re.compile(r"^#{1,6}(?!#)\S")
HEADING_TRAILING_PUNCT = re.compile(r"^#{1,6}\s+.*[。，、：；]$")
HEADING_MANUAL_NUM = re.compile(r"^#{1,6}\s+\d+(\.\d+)*[\s、.]")

# --- 规则 3：代码块 -------------------------------------------------------
FENCE = re.compile(r"^\s*```(.*)$")

# --- 规则 4/5：图注 -------------------------------------------------------
FIG_CAPTION = re.compile(r"^\*\*图\s*(\d+)\*\*")
LIST_ITEM = re.compile(r"^\s*[-*+]\s+\S")
TABLE_ROW = re.compile(r"^\s*\|")

# --- 规则 8：芯片标签 -----------------------------------------------------
NPU_TAG = re.compile(r'<!--\s*npu="([^"]+)"\s*(id\d+)?\s*-->')
NPU_END = re.compile(r"<!--\s*end\s+(id\d+)\s*-->")
TAG_ORDER = ["950", "a3", "910b", "910", "310p", "310b", "x90", "9030"]
TAG_PRODUCT = {
    "950": ["Ascend 950PR", "Ascend 950DT"],
    "a3": ["Atlas A3"],
    "910b": ["Atlas A2"],
    "910": ["Atlas 训练系列产品"],
    "310p": ["Atlas 推理系列产品"],
    "310b": ["Atlas 200I/500 A2"],
    "x90": ["Kirin X90"],
    "9030": ["Kirin 9030"],
}

# --- 规则 10：API 文档章节顺序 -------------------------------------------
API_SECTION_ORDER = [
    "产品支持情况", "功能说明", "函数原型", "参数说明",
    "数据类型", "返回值说明", "约束说明", "调用示例",
]

# --- 规则 11：术语 -------------------------------------------------------
TERM_FIXES = [
    (re.compile(r"(?<!Buffer )\bL0A\b(?! Buffer)"), "L0A Buffer"),
    (re.compile(r"(?<!Buffer )\bL0B\b(?! Buffer)"), "L0B Buffer"),
    (re.compile(r"(?<!Buffer )\bL0C\b(?! Buffer)"), "L0C Buffer"),
    (re.compile(r"(?<!Buffer )\bL1\b(?! Buffer)(?=[^/\w])"), "L1 Buffer"),
    (re.compile(r"\bAICore\b"), "AI Core"),
    (re.compile(r"Atlas训练"), "Atlas 训练"),
    (re.compile(r"→"), "->"),
]

# --- 规则 9：dtype 写法 ---------------------------------------------------
DTYPE_BAD = re.compile(r"\b(int8|Int8|S8|uint8|U8|int16|Int16|fp16|FP16|bf16|BF16|fp32|FP32)\b")
DTYPE_ORDER = [
    "bool", "int4b_t", "int8_t", "uint8_t", "fp4x2_e2m1_t", "fp4x2_e1m2_t",
    "hifloat8_t", "fp8_e8m0_t", "fp8_e5m2_t", "fp8_e4m3fn_t", "int16_t",
    "uint16_t", "half", "bfloat16_t", "int32_t", "uint32_t", "float",
    "complex32", "int64_t", "uint64_t", "double", "complex64",
]

# --- 规则 14：废弃接口 ---------------------------------------------------
DEPRECATED_MAP_PATH = "docs/zh/api/appendix/interface_change_description.md"


def _load_deprecated() -> dict[str, str]:
    """从接口变更说明里读「原接口名 → 新接口名」。读不到就返回空表（不阻断）。"""
    path = config.DEVKIT_ROOT / DEPRECATED_MAP_PATH
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    row = re.compile(r"^\|\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\|\s*`?([^|`]+?)`?\s*\|")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = row.match(line.strip())
        if not m:
            continue
        old, new = m.group(1).strip(), m.group(2).strip()
        if not old or old in {"原接口名", "接口名"} or not new:
            continue
        out[old] = new
    return out


def _in_fence_flags(lines: list[str]) -> list[bool]:
    flags: list[bool] = []
    inside = False
    for line in lines:
        if FENCE.match(line):
            flags.append(True)
            inside = not inside
            continue
        flags.append(inside)
    return flags


SUPPORT_HEADING = re.compile(r"^##\s*产品支持情况")


def _support_section(lines: list[str]) -> list[tuple[int, str]]:
    """取出「## 产品支持情况」章节内的 (行号, 原文)。

    芯片标签的 id 递增、组内顺序这些约束只在产品支持章节成立——参数说明表里
    也会出现 `<!-- npu="950" -->`（标注某参数只在 950 生效），那些标签是就地
    引用，不参与 id 序列。
    """
    out: list[tuple[int, str]] = []
    inside = False
    for i, line in enumerate(lines, 1):
        if line.startswith("## "):
            inside = bool(SUPPORT_HEADING.match(line))
            continue
        if inside:
            out.append((i, line))
    return out


def _in_support_section(lines: list[str], lineno: int) -> bool:
    inside = False
    for i, line in enumerate(lines, 1):
        if i > lineno:
            break
        if line.startswith("## "):
            inside = bool(SUPPORT_HEADING.match(line))
    return inside


def check_markdown(path: str, max_issues: int = 40) -> dict:
    """执行可机器判定的规范检查，返回报告字典。"""
    try:
        p: Path = resolve(path)
    except ValueError as exc:
        return {"path": path, "passed": 0, "total_rules": 0, "issues": [], "error": str(exc)}
    if not p.exists():
        return {"path": path, "passed": 0, "total_rules": 0, "issues": [], "error": "文件不存在"}
    if p.is_dir():
        return {
            "path": path,
            "passed": 0,
            "total_rules": 0,
            "issues": [],
            "error": "这是一个目录，不能按文档检查；请指定具体 .md 文件",
        }

    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    in_fence = _in_fence_flags(lines)
    issues: list[dict] = []
    checked: list[str] = []

    def add(rule: str, lineno: int, msg: str, snippet: str = "", severity: str = "warn") -> None:
        if len(issues) < max_issues:
            issues.append(
                {
                    "rule": rule,
                    "line": lineno,
                    "message": msg,
                    "snippet": snippet[:160],
                    "severity": severity,
                }
            )

    # 1 标点与空格
    checked.append("R1 标点/中英文空格")
    for i, line in enumerate(lines, 1):
        if in_fence[i - 1]:
            continue
        if SPACE_CJK_EN.search(_strip_product_names(line)):
            add("R1 空格", i, "中英文/中文与数字之间不应有空格", line)
        if HALF_PUNCT_IN_CJK.search(line):
            add("R1 标点", i, "中文语境应使用中文标点", line)
        elif HALF_PERIOD_IN_CJK.search(line):
            add("R1 标点", i, "中文语境应使用中文句号「。」", line)

    # 2 标题
    checked.append("R2 标题格式")
    prev_level = 0
    for i, line in enumerate(lines, 1):
        if in_fence[i - 1] or not line.startswith("#"):
            continue
        level = len(line) - len(line.lstrip("#"))
        if HEADING_BAD_SPACE.match(line):
            add("R2 标题空格", i, "标题的 # 与文字之间必须有空格", line)
        if HEADING_TRAILING_PUNCT.match(line):
            add("R2 标题标点", i, "标题末尾不应有标点", line)
        if HEADING_MANUAL_NUM.match(line):
            add("R2 标题编号", i, "标题不要手工编号", line)
        if prev_level and level > prev_level + 1:
            add("R2 标题层级", i, f"标题层级从 h{prev_level} 跳到 h{level}", line)
        if level <= 4:
            prev_level = level

    # 3 代码块
    checked.append("R3 代码块")
    open_fence = None
    for i, line in enumerate(lines, 1):
        m = FENCE.match(line)
        if not m:
            continue
        if open_fence is None:
            open_fence = i
            if not m.group(1).strip():
                add("R3 代码块语言", i, "代码块需声明语言（```cpp / ```python 等）", line)
            elif i > 1 and lines[i - 2].strip():
                add("R3 代码块空行", i, "代码块上方应有空行", line)
        else:
            open_fence = None
            if i < len(lines) and lines[i].strip():
                add("R3 代码块空行", i, "代码块下方应有空行", line)

    # 4/5 图注
    checked.append("R4/R5 图注与编号")
    fig_nums: list[tuple[int, int]] = []
    for i, line in enumerate(lines, 1):
        m = FIG_CAPTION.match(line.strip())
        if m:
            fig_nums.append((int(m.group(1)), i))
    if fig_nums:
        for expect, (num, lineno) in enumerate(fig_nums, 1):
            if num != expect:
                add("R5 图编号", lineno, f"图编号应连续，期望 {expect}，实际 {num}", "")
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith("!["):
            m = re.match(r"!\[[^\]]*\]\(([^)]+)\)", s)
            if m and "\\" in m.group(1):
                add("R4 图片路径", i, "图片路径应使用正斜杠", s)
            # 规范原文：「如果有图注（没有图注的话就忽略）」——所以**没有**图注
            # 不算问题；只有把 `**图 n**` 写在图片下方才是错（应写在图片上方）。
            nxt = lines[i].strip() if i < len(lines) else ""
            if FIG_CAPTION.match(nxt):
                add("R4 图注位置", i, "图注应写在图片上方，格式 **图 n**  标题", nxt)

    # 8 芯片标签
    # 只检查**产品支持情况章节内、顶层**的标签：真实文档里标签可以嵌套
    # （产品支持块里又套了子标签），全局按出现顺序要求 id 严格递增会把官方文档
    # 也报一堆错——那是噪声，不是规范问题。
    checked.append("R8 芯片标签")
    section = _support_section(lines)
    top_group: list[str] = []
    for lineno, line in section:
        depth = len(line) - len(line.lstrip())
        m = NPU_TAG.search(line)
        if not m:
            continue
        tags = [t.strip().lower() for t in re.split(r"[,，/]", m.group(1)) if t.strip()]
        for t in tags:
            if t not in TAG_ORDER:
                add("R8 标签值", lineno, f"芯片标签取值非法：{t}", line, severity="error")
        if depth == 0:
            top_group.extend(tags)
    if top_group:
        idx = [TAG_ORDER.index(t) for t in top_group if t in TAG_ORDER]
        if idx != sorted(idx):
            add(
                "R8 标签顺序", 0,
                f"标签组内顺序应为 {'→'.join(TAG_ORDER)}", ",".join(top_group),
            )

    # 8b 标签 id：规范要求「从 id1 开始、不重复、递增」，是对**整篇文档**的约束。
    # 逐个 mismatch 报会把一篇文档刷出十几条同样的问题，所以这里汇总成最多三条。
    all_ids = [(text.count("\n", 0, m.start()) + 1, m.group(2))
               for m in NPU_TAG.finditer(text) if m.group(2)]
    if all_ids:
        nums = [int(t[2:]) for _, t in all_ids if t[2:].isdigit()]
        dup = sorted({t for _, t in all_ids if [x for _, x in all_ids].count(t) > 1})
        if dup:
            add("R8 标签 id", 0, f"标签 id 重复：{', '.join(dup)}", "", severity="error")
        if nums and nums[0] != 1:
            add("R8 标签 id", all_ids[0][0],
                f"首个标签 id 应为 id1，实际 {all_ids[0][1]}", "")
        if nums != sorted(nums) or len(set(nums)) != len(nums):
            if nums and any(b <= a for a, b in zip(nums, nums[1:])):
                add("R8 标签 id", 0, "标签 id 未按文档顺序递增", ",".join(all_ids[0][1:3]) + "…")

    # 标签块内「- 产品名：支持/不支持」的产品名要与标签一致（仅产品支持章节）
    block_re = re.compile(
        r'<!--\s*npu="([^"]+)"[^>]*-->\s*\n?(.*?)<!--\s*end[^>]*-->', re.S
    )
    for m in block_re.finditer(text):
        lineno = text.count("\n", 0, m.start()) + 1
        if not _in_support_section(lines, lineno):
            continue
        tags = [t.strip().lower() for t in re.split(r"[,，/]", m.group(1)) if t.strip()]
        for tag in tags:
            allowed = TAG_PRODUCT.get(tag)
            if not allowed:
                continue
            body = m.group(2)
            pieces = re.findall(r"^[ \t]*-[ \t]*([^：:\n]+)[：:]", body, re.M)
            if not pieces:
                add("R8 标签块", lineno, f"npu=\"{tag}\" 块内没有「- 产品名：支持/不支持」条目", "")
                continue
            for piece in pieces:
                if not any(a in piece for a in allowed):
                    add(
                        "R8 产品名", lineno,
                        f"npu=\"{tag}\" 块内产品名应与标签一致（含「{allowed[0]}」），"
                        f"实际「{piece.strip()}」", "",
                    )
    for i, line in enumerate(lines, 1):
        m = re.search(r"-\s*(Ascend 950PR/Ascend 950DT|[^：:]+)[：:]\s*(支持|不支持)", line)
        if not m:
            continue
        product = m.group(1)
        if "Atlas A2/A3" in product or "A2/A3" in product:
            add("R8 合并产品名", i, "禁止使用 Atlas A2/A3 合并写法", line)

    # 10 API 文档章节顺序
    # 只对**看起来是 API 文档**的文件检查：`/api/` 下还有大量概述页、列表页、
    # usage 页，它们本来就没有「产品支持情况」「调用示例」，一刀切会刷满误报。
    looks_like_api_doc = "## 函数原型" in text or "## 参数说明" in text
    if "/api/" in path.replace("\\", "/") and looks_like_api_doc:
        checked.append("R10 章节顺序")
        found = []
        for i, line in enumerate(lines, 1):
            m = re.match(r"^##\s+(.+?)\s*$", line)
            if not m:
                continue
            name = m.group(1).strip()
            for s in API_SECTION_ORDER:
                if name.startswith(s):
                    found.append((API_SECTION_ORDER.index(s), s, i))
                    break
        order = [f[0] for f in found]
        if order != sorted(order):
            add("R10 章节顺序", found[0][2] if found else 0,
                "章节顺序应为：" + "→".join(API_SECTION_ORDER), "", severity="error")
        names = [f[1] for f in found]
        if "产品支持情况" not in names:
            add("R10 缺章节", 0, "API 文档缺少「## 产品支持情况」", "", severity="error")
        if "调用示例" not in names:
            add("R10 缺章节", 0, "API 文档缺少「## 调用示例」", "", severity="error")

    # 11 术语（跳过行内代码：`Location::L0C` 是枚举值，不是缓冲区名）
    checked.append("R11 术语")
    for i, line in enumerate(lines, 1):
        if in_fence[i - 1]:
            continue
        probe = _strip_inline_code(line)
        for pat, fix in TERM_FIXES:
            if pat.search(probe):
                add("R11 术语", i, f"应写作「{fix}」", line)

    # 9 dtype 写法（跳过行内代码与链接文字：`[Int8 Channel Merge](...)` 是特性名）
    checked.append("R9 dtype 写法")
    for i, line in enumerate(lines, 1):
        if in_fence[i - 1]:
            continue
        probe = _strip_link_text(_strip_inline_code(line))
        for m in DTYPE_BAD.finditer(probe):
            add(
                "R9 dtype", i,
                f"dtype 应写规范名（{m.group(0)} → 如 int8_t/half/bfloat16_t/float）", line,
            )

    # 13 头文件路径写法
    checked.append("R13 头文件写法")
    for i, line in enumerate(lines, 1):
        if in_fence[i - 1]:
            continue
        if "头文件" in line and "路径" in line and "头文件路径为：" not in line:
            add("R13 头文件", i, "应写作「头文件路径为：`xxx.h`」", line)

    # 14 废弃接口
    checked.append("R14 废弃接口")
    deprecated = _load_deprecated()
    if deprecated:
        for i, line in enumerate(lines, 1):
            if in_fence[i - 1]:
                continue
            for old, new in deprecated.items():
                if re.search(rf"\b{re.escape(old)}\b", line):
                    add("R14 废弃接口", i, f"{old} 已废弃，应改用 {new}", line, severity="error")

    passed = len(checked) - len({i["rule"].split()[0] for i in issues})
    counts = {
        lvl: sum(1 for i in issues if i.get("severity") == lvl)
        for lvl in ("error", "warn")
    }
    return {
        "path": path,
        "passed": max(passed, 0),
        "total_rules": len(checked),
        "issues": issues,
        "counts": counts,
        "checked_rules": checked,
        "error": "",
    }
