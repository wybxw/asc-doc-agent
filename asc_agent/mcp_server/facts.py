"""芯片/架构事实层。

事实源是 `asc-npu-arch` 技能的结构化文件
`references/npu-arch-facts.json`——按该技能「统一维护原则」的要求，
本模块**只读取、不复制**芯片表与 dtype 表。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from asc_agent import config

FACTS_REL = "asc-npu-arch/references/npu-arch-facts.json"

# 用户口语 → npu-arch-facts.json 里的芯片调用名
CHIP_ALIASES: dict[str, str] = {
    "910": "ascend910",
    "910a": "ascend910",
    "ascend910": "ascend910",
    "910b": "ascend910b1",
    "910b1": "ascend910b1",
    "910b2": "ascend910b1",
    "910b3": "ascend910b1",
    "910b4": "ascend910b1",
    "910b2c": "ascend910b1",
    "a2": "ascend910b1",
    "atlasa2": "ascend910b1",
    "a3": "ascend910b1",
    "910_93": "ascend910b1",
    "ascend910_93": "ascend910b1",
    "310p": "ascend310p",
    "310p1": "ascend310p",
    "310p3": "ascend310p",
    "610": "ascend610",
    "310b": "ascend310b1",
    "310b1": "ascend310b1",
    "950": "ascend950pr_9599",
    "950pr": "ascend950pr_9599",
    "950dt": "ascend950pr_9599",
    "ascend950": "ascend950pr_9599",
    "a5": "ascend950pr_9599",
}

# 文档 npu 标签值 → 事实文件里的 chip 调用名（用于与支持矩阵对齐）
TAG_TO_CHIP: dict[str, str] = {
    "950": "ascend950pr_9599",
    "a3": "ascend910b1",
    "910b": "ascend910b1",
    "910": "ascend910",
    "310p": "ascend310p",
    "310b": "ascend310b1",
    "x90": "",
    "9030": "",
}

# 芯片标签在文档里的规定相对顺序（asc-doc-checker 第 8 条）
TAG_ORDER = ["950", "a3", "910b", "910", "310p", "310b", "x90", "9030"]


def normalize_chip(raw: str | None) -> str:
    """把用户口语/文档标签/调用名统一成事实文件里的调用名。

    匹配顺序：精确别名 → 精确调用名 → **某个调用名的前缀** → 最长子串兜底。

    为什么需要「前缀」这一步：`ascend910b` 既是 `ascend910`（1001）的超串、
    又是 `ascend910b1`（2201）的前缀。只做子串匹配会把 `ascend910b` 判成初代
    训练架构 1001——架构号直接答错，而且看起来完全合理，极难发现。
    """
    if not raw:
        return ""
    key = raw.strip().lower().replace(" ", "").replace("-", "_")
    if key in CHIP_ALIASES:
        return CHIP_ALIASES[key]
    known = chip_table()
    if key in known:
        return key
    # 前缀匹配：`ascend910b` → `ascend910b1`。取最短的候选，最接近用户说的那一档。
    prefixed = sorted((n for n in known if n.startswith(key)), key=len)
    if prefixed:
        return prefixed[0]
    for alias in sorted(CHIP_ALIASES, key=len, reverse=True):
        if alias in key:
            return CHIP_ALIASES[alias]
    for name in sorted(known, key=len, reverse=True):
        if name in key:
            return name
    return key


def normalize_tag(raw: str | None) -> str:
    """把芯片名收敛到文档 npu 标签的 8 个取值之一（ascend950/atlas_a3/...）。

    先查精确表，再按**前缀**匹配：这样 `310b2`、`310b4`、`910b2c` 这类逐代 SKU
    不必逐个登记，也能落到正确的标签上（否则会被当成未知芯片而丢掉过滤）。
    """
    if not raw:
        return ""
    key = raw.strip().lower().replace(" ", "").replace("-", "_")
    direct = {
        "950": "ascend950", "950pr": "ascend950", "950dt": "ascend950",
        "ascend950": "ascend950", "a5": "ascend950",
        "a3": "atlas_a3", "atlas_a3": "atlas_a3", "atlasa3": "atlas_a3", "910_93": "atlas_a3",
        "910b": "ascend910b", "ascend910b": "ascend910b", "a2": "ascend910b",
        "910b1": "ascend910b", "910b2": "ascend910b", "910b3": "ascend910b",
        "910b4": "ascend910b", "910b2c": "ascend910b", "atlasa2": "ascend910b",
        "910": "ascend910", "ascend910": "ascend910", "910a": "ascend910",
        "310p": "ascend310p", "ascend310p": "ascend310p", "310p1": "ascend310p",
        "310p3": "ascend310p", "610": "ascend310p", "ascend610": "ascend310p",
        "310b": "ascend310b", "ascend310b": "ascend310b", "310b1": "ascend310b",
        "x90": "ascend_x90", "ascend_x90": "ascend_x90", "kirinx90": "ascend_x90",
        "9030": "ascend9030", "ascend9030": "ascend9030", "kirin9030": "ascend9030",
    }
    if key in direct:
        return direct[key]
    # 前缀兜底：长前缀优先，避免 "910" 抢走 "910b"
    for prefix, tag in TAG_PREFIXES:
        if key.startswith(prefix):
            return tag
    return ""


# 前缀 → 文档标签（按长度降序匹配）
TAG_PREFIXES: list[tuple[str, str]] = [
    ("ascend950", "ascend950"), ("atlasa3", "atlas_a3"), ("ascend910b", "ascend910b"),
    ("ascend910", "ascend910"), ("ascend310p", "ascend310p"), ("ascend310b", "ascend310b"),
    ("ascend_x90", "ascend_x90"), ("ascend9030", "ascend9030"), ("kirin", "ascend_x90"),
    ("910_93", "atlas_a3"), ("910b", "ascend910b"), ("910", "ascend910"),
    ("310p", "ascend310p"), ("310b", "ascend310b"), ("950", "ascend950"),
    ("9030", "ascend9030"), ("x90", "ascend_x90"),
]


@lru_cache(maxsize=1)
def load_facts() -> dict:
    path: Path = config.skills_root() / FACTS_REL
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def chip_table() -> dict[str, dict]:
    return dict(load_facts().get("chips", {}))


def dtype_table() -> dict[str, dict]:
    return dict(load_facts().get("dtypes", {}))


def chip_facts(chip: str | None = None) -> dict:
    """返回芯片 → 架构映射；chip 为空时返回全表。"""
    facts = load_facts()
    chips: dict[str, dict] = facts.get("chips", {})
    if not chip:
        return {
            "total": len(chips),
            "chips": chips,
            "source": facts.get("source", ""),
            "version": facts.get("version"),
        }
    key = normalize_chip(chip)
    item = chips.get(key)
    if item is None:
        return {
            "chip": chip,
            "normalized": key,
            "found": False,
            "known": list(chips),
            "note": "未收录该芯片，按缺失处理，不要臆造架构号。",
        }
    return {"chip": chip, "normalized": key, "found": True, **item}


def dtype_facts(dtype: str | None = None) -> dict:
    """返回 dtype 位宽/对齐/范围，以及按产品族划分的支持列表。"""
    facts = load_facts()
    dtypes: dict[str, dict] = facts.get("dtypes", {})
    product = facts.get("dtype_product_support", {})
    if not dtype:
        return {
            "total": len(dtypes),
            "dtypes": dtypes,
            "product_support": product,
            "shorthands": facts.get("dtype_shorthands", {}),
            "source": facts.get("dtype_source", ""),
        }
    item = dtypes.get(dtype)
    shorthand_note = ""
    if item is None:
        short = facts.get("dtype_shorthands", {}).get(dtype)
        if short:
            # 简写可能指向一个**逻辑类型**（如 S4 → int4b_t），而事实表里登记的是
            # 实际存储类型（int4x2_t）。先按 type 找，找不到再退到 storage_type，
            # 否则会给出「found=false 但又返回了 dtype 名」这种自相矛盾的结果。
            logical = short.get("type", "")
            storage = short.get("storage_type", "")
            if logical in dtypes:
                item = dtypes[logical]
                dtype = logical
            elif storage and storage in dtypes:
                item = dtypes[storage]
                shorthand_note = (
                    f"{dtype} 的逻辑类型是 {logical or '未知'}，"
                    f"事实表登记的是存储类型 {storage}，规格按存储类型给出。"
                )
                dtype = storage
    if item is None:
        return {"dtype": dtype, "found": False, "known": list(dtypes)}
    support = [
        {"family": fam, "products": v.get("products", []), "supported": dtype in v.get("dtypes", [])}
        for fam, v in product.items()
    ]
    result = {"dtype": dtype, "found": True, "spec": item, "product_support": support}
    if shorthand_note:
        result["note"] = shorthand_note
    return result
