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

# 派生产品族：只用文档标签粒度做支持性判断时使用
TAG_LABEL: dict[str, str] = {
    "ascend950": "Ascend 950PR/Ascend 950DT",
    "atlas_a3": "Atlas A3 训练系列产品/Atlas A3 推理系列产品",
    "ascend910b": "Atlas A2 训练系列产品/Atlas A2 推理系列产品",
    "ascend910": "Atlas 训练系列产品",
    "ascend310p": "Atlas 推理系列产品",
    "ascend310b": "Atlas 200I/500 A2 推理产品",
    "ascend_x90": "Kirin X90",
    "ascend9030": "Kirin 9030",
}

# 芯片标签在文档里的规定相对顺序（asc-doc-checker 第 8 条）
TAG_ORDER = ["950", "a3", "910b", "910", "310p", "310b", "x90", "9030"]


def normalize_chip(raw: str | None) -> str:
    """把用户口语/文档标签/调用名统一成事实文件里的调用名。

    匹配顺序：精确命中别名 → 事实文件里的正式调用名 → 最长子串匹配。
    **必须最长优先**：`ascend910b1` 若不先按长度排，会被 `910` 抢先匹配成
    `ascend910`（初代训练架构），架构号从 2201 错成 1001。
    无法识别时原样返回（下游会报「未知芯片」，而不是瞎猜一个架构）。
    """
    if not raw:
        return ""
    key = raw.strip().lower().replace(" ", "").replace("-", "_")
    if key in CHIP_ALIASES:
        return CHIP_ALIASES[key]
    known = chip_table()
    if key in known:
        return key
    for alias in sorted(CHIP_ALIASES, key=len, reverse=True):
        if alias in key:
            return CHIP_ALIASES[alias]
    for name in sorted(known, key=len, reverse=True):
        if name in key:
            return name
    return key


def normalize_tag(raw: str | None) -> str:
    """把芯片名收敛到文档 npu 标签的 8 个取値之一（ascend950/atlas_a3/...）。"""
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
    return direct.get(key, "")


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
    if item is None:
        short = facts.get("dtype_shorthands", {}).get(dtype)
        if short:
            item = dtypes.get(short.get("type", ""))
            dtype = short.get("type", dtype)
    if item is None:
        return {"dtype": dtype, "found": False, "known": list(dtypes)}
    support = [
        {"family": fam, "products": v.get("products", []), "supported": dtype in v.get("dtypes", [])}
        for fam, v in product.items()
    ]
    return {"dtype": dtype, "found": True, "spec": item, "product_support": support}
